from decimal import Decimal
from uuid import uuid4

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.forms import formset_factory
from django.shortcuts import get_object_or_404, redirect, render

from accounts.roles import has_right, right_required
from core.models import StockItem
from core.stock import post_document
from core.stock_models import StockDocument

from .maintenance_models import MaintenanceAction, MaintenanceEvent, MaintenanceLine
from .models import Printer


class MaintenanceForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput)
    note = forms.CharField(
        label="Комментарий", required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2})
    )


class MaintenanceItemChoice(forms.ModelChoiceField):
    def label_from_instance(self, item):
        return f"{item.name} · {item.quantity:g} {item.unit} на складе"


class MaintenanceLineForm(forms.Form):
    action = forms.ModelChoiceField(
        label="Вид работы", queryset=MaintenanceAction.objects.filter(enabled=True)
    )
    item = MaintenanceItemChoice(
        label="Позиция со склада",
        required=False,
        queryset=StockItem.objects.filter(
            is_archived=False, quantity__gt=0, spools__isnull=True
        ).select_related("group"),
    )
    quantity = forms.DecimalField(
        label="Количество",
        min_value=Decimal("0.01"),
        max_digits=10,
        decimal_places=2,
        required=False,
        initial=1,
    )

    def __init__(self, *args, can_writeoff, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_writeoff:
            self.fields["action"].queryset = self.fields["action"].queryset.filter(requires_stock=False)


MaintenanceLines = formset_factory(
    MaintenanceLineForm,
    extra=0,
    min_num=1,
    max_num=20,
    validate_min=True,
    validate_max=True,
    can_delete=True,
)


class MaintenanceActionForm(forms.ModelForm):
    class Meta:
        model = MaintenanceAction
        fields = ["name", "requires_stock", "stock_group", "enabled"]

    def clean(self):
        data = super().clean()
        if data.get("stock_group") and not data.get("requires_stock"):
            self.add_error("requires_stock", "Для выбранной группы включите списание со склада.")
        return data


@transaction.atomic
def record_maintenance(printer_id, data, rows, user):
    printer = Printer.objects.select_for_update().get(pk=printer_id)
    previous = printer.maintenance_events.filter(request_id=data["request_id"]).first()
    if previous:
        return previous

    actions = MaintenanceAction.objects.select_for_update().in_bulk({row["action"].pk for row in rows})
    item_ids = {row["item"].pk for row in rows if row.get("item")}
    items = {
        item.pk: item for item in StockItem.objects.select_for_update().filter(pk__in=item_ids).order_by("pk")
    }
    event = MaintenanceEvent.objects.create(
        printer=printer,
        request_id=data["request_id"],
        print_seconds=printer.print_seconds,
        operator=user.get_username(),
        note=data["note"],
    )
    for row in rows:
        action = actions.get(row["action"].pk)
        if not action or not action.enabled:
            raise ValidationError("Вид работы больше недоступен. Выберите другой.")
        item = items.get(row["item"].pk) if row.get("item") else None
        quantity = row.get("quantity")
        document = None
        if action.requires_stock:
            if not has_right(user, "stock_writeoff"):
                raise PermissionDenied
            if not item or item.is_archived or not quantity or item.spools.exists():
                raise ValidationError(f"«{action.name}»: выберите расходник со склада и укажите количество.")
            if action.stock_group_id and action.stock_group_id != item.group_id:
                raise ValidationError(f"«{action.name}»: выберите позицию из группы «{action.stock_group}».")
            if item.unit.strip().lower().rstrip(".") in {"шт", "штука", "штук", "pcs"} and quantity % 1:
                raise ValidationError(f"«{item.name}»: укажите целое количество штук.")
            document = StockDocument.objects.create(
                item=item,
                kind="writeoff",
                quantity=quantity,
                unit=item.unit,
                created_by=user,
                reason=f"{event} · {printer.name} · {action.name}"[:255],
            )
            try:
                post_document(document.pk, user)
            except ValidationError as exc:
                raise ValidationError([f"«{item.name}»: {msg}" for msg in exc.messages]) from exc
        elif item:
            raise ValidationError(f"«{action.name}» настроено как работа без списания.")
        previous_line = (
            MaintenanceLine.objects.filter(event__printer=printer, action=action)
            .exclude(event=event)
            .order_by("-event__recorded_at", "-event_id")
            .first()
        )
        interval = None
        if previous_line and event.print_seconds >= previous_line.event.print_seconds:
            interval = event.print_seconds - previous_line.event.print_seconds
        MaintenanceLine.objects.create(
            event=event,
            action=action,
            action_name=action.name,
            item_name=item.name if item else "",
            document=document,
            interval_seconds=interval,
        )
    return event


@right_required("view_dashboard")
def maintenance(request, pk):
    printer = get_object_or_404(Printer, pk=pk)
    can_record = has_right(request.user, "maint_done")
    if request.method == "POST" and not can_record:
        raise PermissionDenied
    data = request.POST if request.method == "POST" else None
    form = MaintenanceForm(data, initial={"request_id": uuid4()})
    can_writeoff = has_right(request.user, "stock_writeoff")
    lines = MaintenanceLines(data, prefix="work", form_kwargs={"can_writeoff": can_writeoff})
    valid_submission = data is not None and form.is_valid()
    if (
        valid_submission
        and printer.maintenance_events.filter(request_id=form.cleaned_data["request_id"]).exists()
    ):
        return redirect("printer_maintenance", pk=pk)
    if valid_submission and lines.is_valid():
        rows = [row for row in lines.cleaned_data if row and not row.get("DELETE")]
        try:
            event = record_maintenance(pk, form.cleaned_data, rows, request.user)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f"{event} сохранено в истории принтера.")
            return redirect("printer_maintenance", pk=pk)
    actions = MaintenanceAction.objects.filter(enabled=True)
    if not can_writeoff:
        actions = actions.filter(requires_stock=False)
    return render(
        request,
        "printers/maintenance.html",
        {
            "printer": printer,
            "can_record": can_record,
            "form": form,
            "lines": lines,
            "work_catalog": {a.pk: {"group": a.stock_group_id, "stock": a.requires_stock} for a in actions},
            "work_stock": dict(StockItem.objects.values_list("pk", "group_id")),
            "events": printer.maintenance_events.prefetch_related("lines__document")[:100],
        },
    )


@right_required("studio_settings")
def maintenance_catalog(request, pk=None):
    action = get_object_or_404(MaintenanceAction, pk=pk) if pk else None
    form = MaintenanceActionForm(request.POST if request.method == "POST" else None, instance=action)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Вид работы сохранён.")
        return redirect("maintenance_catalog")
    return render(
        request,
        "printers/maintenance_catalog.html",
        {
            "form": form,
            "action": action,
            "actions": MaintenanceAction.objects.select_related("stock_group"),
        },
    )
