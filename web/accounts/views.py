from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.models import Group
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core import audit

from . import lockout
from .forms import LoginForm, PasswordResetForm, RoleForm, UserCreateForm, UserEditForm
from .roles import BASE_ROLES, RIGHTS, ROLE_ADMIN, right_required

User = get_user_model()
GENERIC_ERROR = "Неверный логин или пароль."


def _locked_message():
    minutes = settings.LOGIN_LOCK_MINUTES
    return f"Слишком много неудачных попыток. Вход с этого адреса закрыт на {minutes} мин."


@login_not_required
def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    ip = lockout.client_ip(request)
    form = LoginForm(request.POST or None)
    error = ""

    if request.method == "POST":
        if lockout.is_locked(ip):
            error = _locked_message()
        elif form.is_valid():
            username = form.cleaned_data["username"]
            user = authenticate(request, username=username, password=form.cleaned_data["password"])
            if user is None:
                audit.log(request, audit.Action.LOGIN_FAILED, username=username)
                if lockout.register_failure(ip):
                    audit.log(request, audit.Action.LOGIN_LOCKED, username=username, target=ip)
                    error = _locked_message()
                else:
                    error = GENERIC_ERROR
            else:
                login(request, user)
                lockout.reset(ip)
                if form.cleaned_data["remember"]:
                    request.session.set_expiry(settings.REMEMBER_DAYS * 86400)
                else:
                    request.session.set_expiry(0)  # до закрытия браузера, но не дольше SESSION_HOURS
                audit.log(request, audit.Action.LOGIN, remember=form.cleaned_data["remember"])
                next_url = request.POST.get("next") or request.GET.get("next")
                if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}):
                    return redirect(next_url)
                return redirect("dashboard")
        else:
            error = GENERIC_ERROR

    return render(
        request,
        "accounts/login.html",
        {
            "form": form,
            "error": error,
            "next": request.GET.get("next", ""),
        },
    )


@require_POST
def logout_view(request):
    audit.log(request, audit.Action.LOGOUT)
    logout(request)
    return redirect("login")


# --- Пользователи -----------------------------------------------------------------------------


def _active_admins():
    return User.objects.filter(is_active=True, groups__name=ROLE_ADMIN).distinct()


@right_required("users_manage")
def users(request):
    qs = User.objects.prefetch_related("groups").order_by("-is_active", "first_name", "username")
    return render(request, "accounts/users.html", {"users": qs})


@right_required("users_manage")
def user_create(request):
    form = UserCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        user = User.objects.create_user(
            username=d["username"], password=d["password1"], first_name=d["first_name"]
        )
        user.groups.set(d["roles"])
        audit.log(request, audit.Action.USER_CREATE, target=user.username, roles=[g.name for g in d["roles"]])
        messages.success(request, f"Пользователь {user.username} создан.")
        return redirect("users")
    return render(request, "accounts/user_form.html", {"form": form, "title": "Новый пользователь"})


@right_required("users_manage")
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    initial = {"first_name": user.first_name, "is_active": user.is_active, "roles": user.groups.all()}
    form = UserEditForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        stays_admin = d["is_active"] and any(g.name == ROLE_ADMIN for g in d["roles"])
        if not stays_admin and not _active_admins().exclude(pk=user.pk).exists():
            form.add_error(None, "Это последний активный администратор: без него некому управлять системой.")
        else:
            user.first_name = d["first_name"]
            user.is_active = d["is_active"]
            user.save()
            user.groups.set(d["roles"])
            audit.log(
                request,
                audit.Action.USER_UPDATE,
                target=user.username,
                roles=[g.name for g in d["roles"]],
                is_active=user.is_active,
            )
            messages.success(request, f"Пользователь {user.username} сохранён.")
            return redirect("users")
    return render(
        request,
        "accounts/user_form.html",
        {
            "form": form,
            "title": f"Пользователь {user.username}",
            "edited": user,
        },
    )


@right_required("users_manage")
def user_password(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = PasswordResetForm(request.POST or None, user=user)
    if request.method == "POST" and form.is_valid():
        user.set_password(form.cleaned_data["password1"])
        user.save()
        if user == request.user:
            update_session_auth_hash(request, user)
        audit.log(request, audit.Action.USER_PASSWORD, target=user.username)
        messages.success(request, f"Пароль пользователя {user.username} изменён.")
        return redirect("users")
    return render(
        request,
        "accounts/user_form.html",
        {
            "form": form,
            "title": f"Новый пароль: {user.username}",
            "edited": user,
            "password_mode": True,
        },
    )


# --- Роли -------------------------------------------------------------------------------------


@right_required("users_manage")
def roles(request):
    groups = Group.objects.annotate(n_users=Count("user")).prefetch_related("permissions").order_by("name")
    rows = []
    for g in groups:
        codes = {p.codename for p in g.permissions.all()}
        rows.append(
            {
                "group": g,
                "rights": [name for code, name in RIGHTS if code in codes],
                "is_base": g.name in BASE_ROLES,
            }
        )
    return render(request, "accounts/roles.html", {"rows": rows})


@right_required("users_manage")
def role_edit(request, pk=None):
    group = get_object_or_404(Group, pk=pk) if pk else None
    initial = {}
    if group:
        initial = {"name": group.name, "rights": list(group.permissions.values_list("codename", flat=True))}
    form = RoleForm(request.POST or None, instance=group, initial=initial)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        action = audit.Action.ROLE_UPDATE if group else audit.Action.ROLE_CREATE
        audit.log(request, action, target=saved.name, rights=form.cleaned_data.get("rights", []))
        messages.success(request, f"Роль «{saved.name}» сохранена.")
        return redirect("roles")
    return render(
        request,
        "accounts/role_form.html",
        {
            "form": form,
            "group": group,
            "is_admin_role": group is not None and group.name == ROLE_ADMIN,
            "can_delete": group is not None and group.name not in BASE_ROLES,
        },
    )


@right_required("users_manage")
@require_POST
def role_delete(request, pk):
    group = get_object_or_404(Group, pk=pk)
    if group.name in BASE_ROLES:
        messages.error(request, "Базовые роли не удаляются.")
    elif group.user_set.exists():
        messages.error(request, "Роль назначена пользователям — сначала снимите её.")
    else:
        name = group.name
        group.delete()
        audit.log(request, audit.Action.ROLE_DELETE, target=name)
        messages.success(request, f"Роль «{name}» удалена.")
    return redirect("roles")
