from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.roles import has_right, right_required, user_rights
from printers.models import Printer

from . import audit
from .models import StockItem
from .stock import correct, post_document, receive_spool
from .stock_forms import CorrectionForm, MovementForm, SpoolForm, StockSetupForm
from .stock_models import PrinterStockSetup, PrintStockRun, Spool, StockDocument, StockMovement


def form_page(request, form, title):
    return render(request, "core/stock_operation.html", {"form": form, "title": title})


@right_required("stock_income", "stock_writeoff")
def item_detail(request, pk):
    item = get_object_or_404(StockItem, pk=pk)
    return render(
        request,
        "core/stock_item_detail.html",
        {
            "item": item,
            "spools": item.spools.all(),
            "orders": item.order_items.filter(order__status__is_final=False),
            "runs": PrintStockRun.objects.filter(spool__item=item, state="active").select_related(
                "printer", "spool"
            ),
            "movements": item.movements.select_related("spool", "created_by")[:100],
        },
    )


@right_required("stock_income")
def spool_add(request):
    form = SpoolForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            spool = receive_spool(form.save(commit=False), request.user, form.cleaned_data["opening"])
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            audit.log(request, "stock_spool_income", target=spool.code)
            messages.success(request, "Катушка зарегистрирована. Вес учтён на складе.")
            return redirect("stock")
    return form_page(request, form, "Приход / начальная регистрация катушки")


@right_required("stock_income")
def spool_edit(request, pk):
    spool = get_object_or_404(Spool, pk=pk)
    form = SpoolForm(request.POST or None, instance=spool)
    for name in ("item", "nominal_g", "remaining_g", "purchase_price"):
        form.fields[name].disabled = True
    form.fields.pop("opening")
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            Spool.objects.select_for_update().get(pk=pk)
            # Не сохраняем старый вес, который мог измениться в коллекторе.
            Spool.objects.filter(pk=pk).update(
                code=form.cleaned_data["code"],
                tare_g=form.cleaned_data["tare_g"],
                location=form.cleaned_data["location"],
                status=form.cleaned_data["status"],
            )
            audit.log(request, "stock_spool_update", target=spool.code)
        return redirect("stock")
    return form_page(request, form, f"Катушка {spool.code}: место и тара; вес меняется документом")


@right_required("print_control")
def setup(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    instance = PrinterStockSetup.objects.filter(printer=printer).first() or PrinterStockSetup(printer=printer)
    form = StockSetupForm(request.POST or None, instance=instance)
    # Оператору не раскрываем список клиентов и заказов через подписи ModelChoice.
    if "orders_full" not in user_rights(request.user):
        form.fields["order_item"].label_from_instance = lambda item: f"Изделие #{item.pk} · {item.name}"
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            Printer.objects.select_for_update().get(pk=pk)
            if PrintStockRun.objects.filter(printer=printer, state="active").exists():
                form.add_error(None, "Принтер уже печатает: привязка заблокирована.")
            else:
                spool = form.cleaned_data["spool"]
                if spool:
                    Spool.objects.select_for_update().get(pk=spool.pk)
                if spool and PrinterStockSetup.objects.filter(spool=spool).exclude(printer=printer).exists():
                    form.add_error("spool", "Катушка уже установлена на другой принтер.")
                else:
                    PrinterStockSetup.objects.update_or_create(printer=printer, defaults=form.cleaned_data)
                    audit.log(request, "printer_stock_setup", target=printer.name)
                    return redirect("printer_detail", pk=pk)
    return form_page(request, form, f"{printer.name}: катушка и выпуск на следующую печать")


@right_required("stock_income", "stock_writeoff")
def operation(request):
    form = MovementForm(request.POST or None)
    rights = user_rights(request.user)
    form.fields["kind"].choices = [
        c
        for c in form.fields["kind"].choices
        if (c[0] == "writeoff" and "stock_writeoff" in rights)
        or (c[0] == "income" and "stock_income" in rights)
        or (c[0] == "count" and "studio_settings" in rights)
    ]
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                doc = StockDocument.objects.create(**data, unit=data["item"].unit, created_by=request.user)
                if request.POST.get("save") != "draft":
                    post_document(doc.pk, request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            audit.log(request, "stock_document", target=str(doc), kind=data["kind"])
            messages.success(
                request,
                "Черновик сохранён."
                if request.POST.get("save") == "draft"
                else "Документ проведён. Остаток обновлён.",
            )
            return redirect("stock")
    return render(
        request, "core/stock_operation.html", {"form": form, "title": "Документ склада", "draftable": True}
    )


@right_required("stock_income", "stock_writeoff")
@require_POST
def document_post(request, pk):
    doc = get_object_or_404(StockDocument, pk=pk)
    required = {"income": "stock_income", "writeoff": "stock_writeoff", "count": "studio_settings"}[doc.kind]
    if not has_right(request.user, required):
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden("Нет права на проведение этой операции.")
    try:
        post_document(pk, request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("stock")


@right_required("studio_settings")
def correction(request, pk):
    run = get_object_or_404(PrintStockRun, pk=pk)
    form = CorrectionForm(request.POST or None, initial={"weight": run.consumed_g})
    if request.method == "POST" and form.is_valid():
        try:
            correct(run.pk, form.cleaned_data["weight"], form.cleaned_data["reason"], request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            audit.log(
                request, "stock_print_correction", target=run.filename, grams=str(form.cleaned_data["weight"])
            )
            return redirect("stock")
    return form_page(request, form, f"Админка склада: расход «{run.filename}»")


@right_required("stock_income", "stock_writeoff")
def stock(request):
    tab = request.GET.get("tab", "balances")
    items = StockItem.objects.select_related("group").prefetch_related("spools")
    query = request.GET.get("q", "").strip()
    if query:
        items = items.filter(name__icontains=query)
    selected_group = request.GET.get("group", "")
    if selected_group.isdigit():
        items = items.filter(group_id=int(selected_group))
    archived = request.GET.get("archive") == "1"
    items = items.filter(is_archived=archived)
    rows = []
    for item in items:
        reserve = item.reserved_grams() if item.grams_per_unit else Decimal("0")
        available = item.quantity - reserve / item.grams_per_unit if item.grams_per_unit else item.quantity
        low = item.min_quantity is not None and available < item.min_quantity
        rows.append(
            {
                "item": item,
                "reserved": reserve / item.grams_per_unit if item.grams_per_unit else 0,
                "available": available,
                "low": low,
            }
        )
    if tab == "reorder":
        rows = [r for r in rows if r["low"]]
    from .models import StockGroup

    return render(
        request,
        "core/stock.html",
        {
            "tab": tab,
            "selected_group": selected_group,
            "rows": rows,
            "query": query,
            "archived": archived,
            "groups": StockGroup.objects.prefetch_related("items"),
            "spools": Spool.objects.select_related("item", "installed_on__printer"),
            "movements": StockMovement.objects.select_related("item", "spool", "created_by")[:150],
            "drafts": StockDocument.objects.filter(movement=None).select_related("item", "spool")[:100],
            "runs": PrintStockRun.objects.select_related("printer", "spool", "job")[:100],
            "printers": Printer.objects.all(),
            "can_correct": has_right(request.user, "studio_settings"),
        },
    )
