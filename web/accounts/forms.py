from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.models import Group, Permission, User

from .roles import APP, RIGHTS, ROLE_ADMIN

PASSWORD_HELP = "Не короче 8 символов, не только цифры, не слишком простой и не похожий на логин."


class LoginForm(forms.Form):
    username = forms.CharField(label="Логин", max_length=150)
    password = forms.CharField(label="Пароль", strip=False, widget=forms.PasswordInput)
    remember = forms.BooleanField(label="Запомнить меня на этом устройстве", required=False)


class PasswordPairMixin:
    """Пароль и повтор, с проверкой валидаторами Django."""

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Пароли не совпадают.")
        elif p1:
            user = getattr(self, "user_for_validation", None) or User(
                username=data.get("username", ""), first_name=data.get("first_name", "")
            )
            try:
                password_validation.validate_password(p1, user)
            except forms.ValidationError as e:
                self.add_error("password1", e)
        return data


def password_fields():
    return (
        forms.CharField(
            label="Пароль",
            strip=False,
            widget=forms.PasswordInput,
            help_text=PASSWORD_HELP,
        ),
        forms.CharField(label="Пароль ещё раз", strip=False, widget=forms.PasswordInput),
    )


class RolesField(forms.ModelMultipleChoiceField):
    def __init__(self, **kwargs):
        super().__init__(
            queryset=Group.objects.order_by("name"),
            widget=forms.CheckboxSelectMultiple,
            label="Роли",
            **kwargs,
        )


class UserCreateForm(PasswordPairMixin, forms.Form):
    first_name = forms.CharField(label="Имя", max_length=150)
    username = forms.RegexField(
        label="Логин",
        max_length=150,
        regex=r"^[A-Za-z0-9_.@+-]+\Z",
        error_messages={"invalid": "Только латинские буквы, цифры и символы . @ + - _"},
    )
    password1, password2 = password_fields()
    roles = RolesField(required=True)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Такой логин уже есть.")
        return username


class UserEditForm(forms.Form):
    first_name = forms.CharField(label="Имя", max_length=150)
    is_active = forms.BooleanField(label="Может входить в систему", required=False)
    roles = RolesField(required=True)


class PasswordResetForm(PasswordPairMixin, forms.Form):
    password1, password2 = password_fields()

    def __init__(self, *args, user=None, **kwargs):
        self.user_for_validation = user
        super().__init__(*args, **kwargs)


class RoleForm(forms.Form):
    name = forms.CharField(label="Название роли", max_length=150)
    rights = forms.MultipleChoiceField(
        label="Права", choices=RIGHTS, widget=forms.CheckboxSelectMultiple, required=False
    )

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)
        if instance is not None and instance.name == ROLE_ADMIN:
            self.fields["name"].disabled = True
            self.fields["rights"].disabled = True

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Group.objects.filter(name__iexact=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Роль с таким названием уже есть.")
        return name

    def save(self):
        group = self.instance or Group()
        if group.name != ROLE_ADMIN:
            group.name = self.cleaned_data["name"]
        group.save()
        if group.name != ROLE_ADMIN:
            perms = Permission.objects.filter(
                content_type__app_label=APP, codename__in=self.cleaned_data["rights"]
            )
            group.permissions.set(perms)
        return group
