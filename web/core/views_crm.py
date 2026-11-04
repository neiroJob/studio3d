"""CRM: клиенты и заказы (ROADMAP.md, «CRM»).

Права: `orders_full` видит всё; `orders_anonymized` (оператор) видит заказ без клиента, контактов,
цены, себестоимости и платежей и не может их менять.
"""

import os
from decimal import ROUND_HALF_UP, Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.roles import has_right, right_required

from . import audit
from .forms import (
    ClientDocumentForm,
    ClientForm,
    ClientNoteForm,
    CommentForm,
    ContactForm,
    CostLineForm,
    OrderFileForm,
    OrderForm,
    OrderItemForm,
    PaymentForm,
    WorkStageForm,
)
from .models import (
    Client,
    ClientDocument,
    ClientNote,
    ContactPerson,
    Order,
    OrderChecklistItem,
    OrderCostLine,
    OrderEvent,
    OrderFile,
    OrderItem,
    OrderPayment,
    OrderStatus,
    WorkStage,
)

Action = audit.Action
orders_any = right_required("orders_full", "orders_anonymized")
orders_full = right_required("orders_full")
stages_admin = right_required("studio_settings")  # общий список этапов меняет только Админ


def _back(name, *args, **query):
    url = reverse(name, args=args)
    return f"{url}?{'&'.join(f'{k}={v}' for k, v in query.items())}" if query else url


def _flash_errors(request, form):
    for errors in form.errors.values():
        for error in errors:
            messages.error(request, error)


def _form_page(request, form, title, back, *, multipart=False, submit="Сохранить"):
    return render(
        request,
        "core/crm_form.html",
        {"form": form, "title": title, "back": back, "multipart": multipart, "submit": submit},
    )


def _event(order, user, kind, text):
    OrderEvent.objects.create(order=order, user=user, kind=kind, text=text)


# --- Клиенты -----------------------------------------------------------------------------------


@orders_full
def clients(request):
    q = request.GET.get("q", "").strip()
    kind = request.GET.get("kind", "")
    archived = request.GET.get("archived") == "1"
    qs = Client.objects.filter(is_archived=archived).annotate(
        orders_count=Count("orders", distinct=True), last_order=Max("orders__created_at")
    )
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(inn__icontains=q)
            | Q(tags__icontains=q)
            | Q(contacts__name__icontains=q)
            | Q(contacts__phone__icontains=q)
            | Q(contacts__email__icontains=q)
        ).distinct()
    if kind in Client.Kind.values:
        qs = qs.filter(kind=kind)
    page = Paginator(qs.order_by("name", "pk"), 50).get_page(request.GET.get("page"))
    return render(
        request,
        "core/clients.html",
        {"page": page, "q": q, "kind": kind, "archived": archived, "kinds": Client.Kind.choices},
    )


@orders_full
def client_edit(request, pk=None):
    client = get_object_or_404(Client, pk=pk) if pk else None
    form = ClientForm(request.POST or None, instance=client)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        action = Action.CLIENT_UPDATE if client else Action.CLIENT_CREATE
        audit.log(request, action, target=saved.name, kind=saved.kind)
        messages.success(request, f"Клиент «{saved.name}» сохранён.")
        return redirect("client_detail", pk=saved.pk)
    return render(request, "core/client_form.html", {"form": form, "client": client})


@orders_full
def client_detail(request, pk):
    client = get_object_or_404(Client.objects.prefetch_related("contacts"), pk=pk)
    tab = request.GET.get("tab", "orders")
    if tab not in ("orders", "docs", "notes"):
        tab = "orders"
    orders = list(client.orders.select_related("status").prefetch_related("payments"))
    billable = [o for o in orders if not (o.status.is_final and o.status.is_side)]  # без отменённых
    return render(
        request,
        "core/client_detail.html",
        {
            "client": client,
            "tab": tab,
            "orders": orders,
            "orders_total": sum(o.price for o in billable),
            "debt": sum(o.rest for o in billable),
            "documents": client.documents.all(),
            "notes": client.notes.select_related("author"),
            "note_form": ClientNoteForm(),
        },
    )


@orders_full
@require_POST
def client_archive(request, pk):
    client = get_object_or_404(Client, pk=pk)
    client.is_archived = not client.is_archived
    client.save(update_fields=["is_archived"])
    audit.log(request, Action.CLIENT_ARCHIVE, target=client.name, is_archived=client.is_archived)
    verb = "перемещён в архив" if client.is_archived else "восстановлен"
    messages.success(request, f"Клиент «{client.name}» {verb}.")
    return redirect("client_detail", pk=client.pk)


@orders_full
def contact_edit(request, client_pk=None, pk=None):
    contact = get_object_or_404(ContactPerson.objects.select_related("client"), pk=pk) if pk else None
    client = contact.client if contact else get_object_or_404(Client, pk=client_pk)
    form = ContactForm(request.POST or None, instance=contact)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.client = client
        saved.save()
        audit.log(request, Action.CLIENT_CONTACT, target=f"{client.name}: {saved.name}", saved=True)
        messages.success(request, f"Контакт «{saved.name}» сохранён.")
        return redirect("client_detail", pk=client.pk)
    title = f"Контакт «{contact.name}»" if contact else f"Новый контакт — {client.name}"
    return _form_page(request, form, title, _back("client_detail", client.pk))


@orders_full
@require_POST
def contact_delete(request, pk):
    contact = get_object_or_404(ContactPerson.objects.select_related("client"), pk=pk)
    client, name = contact.client, contact.name
    contact.delete()  # в заказах контакт обнуляется (SET_NULL)
    audit.log(request, Action.CLIENT_CONTACT, target=f"{client.name}: {name}", deleted=True)
    messages.success(request, f"Контакт «{name}» удалён.")
    return redirect("client_detail", pk=client.pk)


@orders_full
@require_POST
def note_add(request, pk):
    client = get_object_or_404(Client, pk=pk)
    form = ClientNoteForm(request.POST)
    if form.is_valid():
        note = form.save(commit=False)
        note.client, note.author = client, request.user
        note.save()
        messages.success(request, "Заметка добавлена.")
    else:
        _flash_errors(request, form)
    return redirect(_back("client_detail", client.pk, tab="notes"))


@orders_full
@require_POST
def note_delete(request, pk):
    note = get_object_or_404(ClientNote, pk=pk)
    client_pk = note.client_id
    note.delete()
    messages.success(request, "Заметка удалена.")
    return redirect(_back("client_detail", client_pk, tab="notes"))


@orders_full
def document_add(request, pk):
    client = get_object_or_404(Client, pk=pk)
    form = ClientDocumentForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        doc = form.save(commit=False)
        doc.client, doc.uploaded_by = client, request.user
        doc.save()
        audit.log(request, Action.CLIENT_DOCUMENT, target=f"{client.name}: {doc.display_name}")
        messages.success(request, "Документ загружен.")
        return redirect(_back("client_detail", client.pk, tab="docs"))
    return _form_page(
        request,
        form,
        f"Документ клиента — {client.name}",
        _back("client_detail", client.pk, tab="docs"),
        multipart=True,
        submit="Загрузить",
    )


@orders_full
def document_download(request, pk):
    doc = get_object_or_404(ClientDocument, pk=pk)
    return _download(doc.file)


@orders_full
@require_POST
def document_delete(request, pk):
    doc = get_object_or_404(ClientDocument.objects.select_related("client"), pk=pk)
    client, name = doc.client, doc.display_name
    doc.file.delete(save=False)
    doc.delete()
    audit.log(request, Action.CLIENT_DOCUMENT, target=f"{client.name}: {name}", deleted=True)
    messages.success(request, "Документ удалён.")
    return redirect(_back("client_detail", client.pk, tab="docs"))


def _download(field):
    if not field or not field.storage.exists(field.name):
        raise Http404
    return FileResponse(field.open("rb"), as_attachment=True, filename=os.path.basename(field.name))


# --- Заказы ------------------------------------------------------------------------------------


@orders_any
def orders(request):
    full = has_right(request.user, "orders_full")
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    mine = request.GET.get("mine") == "1"
    show_closed = request.GET.get("closed") == "1"
    qs = Order.objects.select_related("client", "status", "responsible").prefetch_related(
        "payments", "cost_lines", "checklist"
    )
    if q:
        cond = Q(number__icontains=q) | Q(title__icontains=q)
        if full:  # оператор не ищет по данным клиента
            cond |= Q(client__name__icontains=q)
        qs = qs.filter(cond)
    if status.isdigit():
        qs = qs.filter(status_id=int(status))
    elif not show_closed:
        qs = qs.filter(status__is_final=False)
    if mine:
        qs = qs.filter(responsible=request.user)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "core/orders.html",
        {
            "page": page,
            "q": q,
            "status": status,
            "mine": mine,
            "show_closed": show_closed,
            "statuses": OrderStatus.objects.all(),
            "full": full,
        },
    )


@orders_full
def order_edit(request, pk=None):
    order = get_object_or_404(Order, pk=pk) if pk else None
    initial = {}
    if not order and request.GET.get("client", "").isdigit():
        initial["client"] = int(request.GET["client"])
    form = OrderForm(request.POST or None, instance=order, initial=initial)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        if not order:
            saved.status = OrderStatus.first()
            saved.created_by = request.user
        with transaction.atomic():
            saved.save()
            if not order:  # этапы копируются из общего списка; позже список меняется только для новых заказов
                OrderChecklistItem.objects.bulk_create(
                    OrderChecklistItem(order=saved, title=stage.name) for stage in WorkStage.objects.all()
                )
        if order:
            audit.log(request, Action.ORDER_UPDATE, target=saved.number, price=str(saved.price))
            _event(saved, request.user, OrderEvent.Kind.SYSTEM, "Параметры заказа изменены.")
        else:
            audit.log(request, Action.ORDER_CREATE, target=saved.number, client=saved.client.name)
            _event(saved, request.user, OrderEvent.Kind.SYSTEM, f"Заказ создан, статус «{saved.status}».")
        messages.success(request, f"Заказ {saved.number} сохранён.")
        return redirect("order_detail", pk=saved.pk)
    return render(request, "core/order_form.html", {"form": form, "order": order})


@orders_any
def order_detail(request, pk):
    full = has_right(request.user, "orders_full")
    order = get_object_or_404(
        Order.objects.select_related("client", "contact", "status", "responsible").prefetch_related(
            "items", "payments__created_by", "cost_lines", "checklist", "files", "events__user"
        ),
        pk=pk,
    )
    tabs = ["items", "files", "log"] + (["money"] if full else [])
    tab = request.GET.get("tab", "items")
    if tab not in tabs:
        tab = "items"

    statuses = list(OrderStatus.objects.all())
    chain = [s for s in statuses if not s.is_side]
    current = next((i for i, s in enumerate(chain) if s.pk == order.status_id), None)
    stepper = [
        {
            "status": s,
            "state": "cur" if i == current else "done" if current is not None and i < current else "todo",
        }
        for i, s in enumerate(chain)
    ]
    events = list(order.events.all())
    if not full:  # деньги оператору не показываем
        events = [e for e in events if e.kind != OrderEvent.Kind.MONEY]
    files = {}
    for f in order.files.all():
        files.setdefault(f.title, []).append(f)
    done, total = order.checklist_progress
    return render(
        request,
        "core/order_detail.html",
        {
            "order": order,
            "full": full,
            "tab": tab,
            "tabs": tabs,
            "stepper": stepper,
            "side_statuses": [s for s in statuses if s.is_side],
            "events": events,
            "file_groups": list(files.values()),
            "check_done": done,
            "check_total": total,
            "comment_form": CommentForm(),
            "items_total": sum(i.total for i in order.items.all()),
        },
    )


def _order_back(order_pk, tab):
    return _back("order_detail", order_pk, tab=tab)


@orders_any
@require_POST
def order_status(request, pk):
    order = get_object_or_404(Order.objects.select_related("status"), pk=pk)
    status = get_object_or_404(OrderStatus, pk=request.POST.get("status", 0) or 0)
    if status.pk != order.status_id:
        old = order.status
        order.status = status
        order.save(update_fields=["status"])
        _event(order, request.user, OrderEvent.Kind.STATUS, f"Статус: «{old}» → «{status}».")
        audit.log(request, Action.ORDER_STATUS, target=order.number, old=old.name, new=status.name)
        messages.success(request, f"Статус заказа {order.number}: «{status}».")
    return redirect("order_detail", pk=order.pk)


@orders_any
@require_POST
def order_comment(request, pk):
    order = get_object_or_404(Order, pk=pk)
    form = CommentForm(request.POST)
    if form.is_valid():
        _event(order, request.user, OrderEvent.Kind.COMMENT, form.cleaned_data["text"])
    else:
        _flash_errors(request, form)
    return redirect(_order_back(order.pk, "log"))


# Изделия


@orders_full
def item_edit(request, order_pk=None, pk=None):
    item = get_object_or_404(OrderItem.objects.select_related("order"), pk=pk) if pk else None
    order = item.order if item else get_object_or_404(Order, pk=order_pk)
    form = OrderItemForm(request.POST or None, request.FILES or None, instance=item, order=order)
    if request.method == "POST" and form.is_valid():
        saved = form.save(user=request.user)
        if form.created_file:
            f = form.created_file
            _event(order, request.user, OrderEvent.Kind.SYSTEM, f"Файл «{f.title}», версия {f.version}.")
            audit.log(request, Action.ORDER_FILE, target=order.number, file=f.title, version=f.version)
        audit.log(request, Action.ORDER_ITEM, target=order.number, item=saved.name, quantity=saved.quantity)
        messages.success(request, f"Изделие «{saved.name}» сохранено.")
        if form.meta_missing:
            messages.warning(
                request, "Время или вес в файле не найдены (слайсер их не записал). Введите вручную."
            )
        if saved.plastic_id and saved.unit_weight_g and saved.plastic.grams_per_unit is None:
            messages.warning(
                request,
                f"«{saved.plastic}» учитывается в «{saved.plastic.unit}»: расход в граммах не сверить "
                "с остатком и не списать автоматически.",
            )
        shortage = saved.shortage_grams
        if shortage > 0:
            messages.warning(
                request,
                f"Пластика «{saved.plastic}» на складе не хватает: нужно ещё {shortage:g} г "
                "сверх остатка с учётом резерва под другие заказы.",
            )
        return redirect(_order_back(order.pk, "items"))
    title = f"Изделие «{item.name}»" if item else f"Новое изделие — {order.number}"
    return _form_page(request, form, title, _order_back(order.pk, "items"), multipart=True)


@orders_full
@require_POST
def item_delete(request, pk):
    item = get_object_or_404(OrderItem.objects.select_related("order"), pk=pk)
    order, name = item.order, item.name
    item.delete()
    audit.log(request, Action.ORDER_ITEM, target=order.number, item=name, deleted=True)
    messages.success(request, f"Изделие «{name}» удалено.")
    return redirect(_order_back(order.pk, "items"))


@orders_any
@require_POST
def item_printed(request, pk):
    """Отметка прогресса печати: оператор видит изделия без цен, но отмечает напечатанное."""
    item = get_object_or_404(OrderItem.objects.select_related("order"), pk=pk)
    try:
        printed = int(request.POST.get("printed", ""))
    except ValueError:
        messages.error(request, "Укажите число напечатанных изделий.")
    else:
        with transaction.atomic():
            delta = max(0, min(printed, item.quantity)) - item.printed
            item.printed += delta
            item.save(update_fields=["printed"])
            _write_off_plastic(request, item, delta)
        audit.log(request, Action.ORDER_ITEM, target=item.order.number, item=item.name, printed=item.printed)
    return redirect(_order_back(item.order_id, "items"))


def _write_off_plastic(request, item, delta):
    """Списание пластика со склада по изменению счётчика напечатанного (минус — возврат на склад).

    Резерв пересчитывается сам: он считается по ещё не напечатанной части. Если единица позиции
    не вес (катушки, штуки), граммы не пересчитать — тогда остаток правят вручную.
    """
    if item.stock_runs.exists() or (item.plastic_id and item.plastic.spools.exists()):
        # Расход уже привязан к печати. Изменение количества изделий не списывает его второй раз.
        return
    stock = item.plastic
    if not delta or stock is None or not item.unit_weight_g or stock.grams_per_unit is None:
        return
    grams = item.unit_weight_g * delta
    change = (grams / stock.grams_per_unit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    stock.quantity = max(stock.quantity - change, Decimal("0"))
    stock.save(update_fields=["quantity"])
    verb = "Списано со склада" if delta > 0 else "Возвращено на склад"
    text = f"{verb}: {abs(grams):g} г пластика «{stock.name}» (изделие «{item.name}»)."
    _event(item.order, request.user, OrderEvent.Kind.SYSTEM, text)
    audit.log(
        request,
        Action.ORDER_STOCK,
        target=item.order.number,
        stock=stock.name,
        grams=str(grams),
        printed_delta=delta,
    )
    if stock.is_low:
        messages.warning(request, f"Пластика «{stock.name}» мало: остаток {stock.quantity:g} {stock.unit}.")


# Деньги


@orders_full
def payment_add(request, pk):
    order = get_object_or_404(Order, pk=pk)
    form = PaymentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        payment = form.save(commit=False)
        payment.order, payment.created_by = order, request.user
        payment.save()
        text = f"Платёж {payment.amount} ₽ ({payment.get_method_display()})."
        _event(order, request.user, OrderEvent.Kind.MONEY, text)
        audit.log(request, Action.ORDER_PAYMENT, target=order.number, amount=str(payment.amount))
        messages.success(request, "Платёж добавлен.")
        return redirect(_order_back(order.pk, "money"))
    return _form_page(
        request, form, f"Платёж по заказу {order.number}", _order_back(order.pk, "money"), submit="Добавить"
    )


@orders_full
@require_POST
def payment_delete(request, pk):
    payment = get_object_or_404(OrderPayment.objects.select_related("order"), pk=pk)
    order, amount = payment.order, payment.amount
    payment.delete()
    _event(order, request.user, OrderEvent.Kind.MONEY, f"Платёж {amount} ₽ удалён.")
    audit.log(request, Action.ORDER_PAYMENT, target=order.number, amount=str(amount), deleted=True)
    messages.success(request, "Платёж удалён.")
    return redirect(_order_back(order.pk, "money"))


@orders_full
def cost_add(request, pk):
    order = get_object_or_404(Order, pk=pk)
    form = CostLineForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        line = form.save(commit=False)
        line.order = order
        line.save()
        audit.log(request, Action.ORDER_COST, target=order.number, kind=line.kind, amount=str(line.amount))
        messages.success(request, "Статья добавлена.")
        return redirect(_order_back(order.pk, "money"))
    return _form_page(
        request,
        form,
        f"Статья себестоимости — {order.number}",
        _order_back(order.pk, "money"),
        submit="Добавить",
    )


@orders_full
@require_POST
def cost_delete(request, pk):
    line = get_object_or_404(OrderCostLine.objects.select_related("order"), pk=pk)
    order = line.order
    audit.log(request, Action.ORDER_COST, target=order.number, kind=line.kind, deleted=True)
    line.delete()
    messages.success(request, "Статья удалена.")
    return redirect(_order_back(order.pk, "money"))


# Этапы работ


@orders_any
@require_POST
def checklist_toggle(request, pk):
    item = get_object_or_404(OrderChecklistItem, pk=pk)
    item.done = not item.done
    item.save(update_fields=["done"])
    if item.done:
        _event(item.order, request.user, OrderEvent.Kind.SYSTEM, f"Этап выполнен: {item.title}.")
    return redirect("order_detail", pk=item.order_id)


@stages_admin
def stages(request):
    """Общий список этапов: только Админ. Менеджер и оператор отмечают этапы в заказе, но не правят список."""
    form = WorkStageForm()
    if request.method == "POST":
        form = WorkStageForm(request.POST)
        if form.is_valid():
            stage = form.save(commit=False)
            stage.order = (WorkStage.objects.aggregate(m=Max("order"))["m"] or 0) + 1
            stage.save()
            audit.log(request, Action.STAGE_CATALOG, target=stage.name, added=True)
            messages.success(request, f"Этап «{stage.name}» добавлен. Он появится в новых заказах.")
            return redirect("stages")
    return render(request, "core/stages.html", {"stages": WorkStage.objects.all(), "form": form})


@stages_admin
@require_POST
def stage_delete(request, pk):
    stage = get_object_or_404(WorkStage, pk=pk)
    audit.log(request, Action.STAGE_CATALOG, target=stage.name, deleted=True)
    stage.delete()
    messages.success(request, f"Этап «{stage.name}» удалён из списка. В существующих заказах он остаётся.")
    return redirect("stages")


@stages_admin
@require_POST
def stage_move(request, pk):
    stage = get_object_or_404(WorkStage, pk=pk)
    ordered = list(WorkStage.objects.all())
    index = ordered.index(stage)
    target = index + (-1 if request.POST.get("dir") == "up" else 1)
    if 0 <= target < len(ordered):
        ordered[index], ordered[target] = ordered[target], ordered[index]
        for position, item in enumerate(ordered, start=1):  # нумеруем заново: порядок мог совпадать
            if item.order != position:
                item.order = position
                item.save(update_fields=["order"])
        audit.log(request, Action.STAGE_CATALOG, target=stage.name, moved=request.POST.get("dir"))
    return redirect("stages")


# Файлы


@orders_any
def file_add(request, pk):
    order = get_object_or_404(Order, pk=pk)
    form = OrderFileForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        f = form.save(commit=False)
        f.order, f.uploaded_by = order, request.user
        last = order.files.filter(title=f.title).aggregate(m=Max("version"))["m"] or 0
        f.version = last + 1
        f.save()
        _event(order, request.user, OrderEvent.Kind.SYSTEM, f"Файл «{f.title}», версия {f.version}.")
        audit.log(request, Action.ORDER_FILE, target=order.number, file=f.title, version=f.version)
        messages.success(request, f"Файл «{f.title}» загружен (версия {f.version}).")
        return redirect(_order_back(order.pk, "files"))
    return _form_page(
        request,
        form,
        f"Файл заказа {order.number}",
        _order_back(order.pk, "files"),
        multipart=True,
        submit="Загрузить",
    )


@orders_any
def file_download(request, pk):
    return _download(get_object_or_404(OrderFile, pk=pk).file)


@orders_full
@require_POST
def file_delete(request, pk):
    f = get_object_or_404(OrderFile.objects.select_related("order"), pk=pk)
    order, title, version = f.order, f.title, f.version
    f.file.delete(save=False)
    f.delete()
    audit.log(request, Action.ORDER_FILE, target=order.number, file=title, version=version, deleted=True)
    messages.success(request, f"Файл «{title}», версия {version} удалён.")
    return redirect(_order_back(order.pk, "files"))
