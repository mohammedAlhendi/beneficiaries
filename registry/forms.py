import datetime as dt
import re

from django import forms
from django.conf import settings

from .importer import FIELD_LABELS, FIELDS, REGISTRY_FIELDS, REGISTRY_REQUIRED, REQUIRED_FIELDS
from .models import AidType, Beneficiary, Distribution, Project
from .roles import visible_projects
from .textutils import clean_id, clean_text, is_valid_id

DATE_WIDGET = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


def _limit_projects(form, user):
    """يحصر قوائم المشاريع في النموذج بما يملكه المستخدم."""
    if user is not None and "project" in form.fields:
        form.fields["project"].queryset = visible_projects(user)


class MonthField(forms.Field):
    """حقل شهر بصيغة YYYY-MM (input type=month) يُعاد كأول يوم في الشهر."""
    widget = forms.TextInput(attrs={"type": "month"})

    def to_python(self, value):
        value = (value or "").strip()
        if not value:
            return None
        m = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise forms.ValidationError("صيغة الشهر غير صالحة")
        return dt.date(int(m.group(1)), int(m.group(2)), 1)


class FilterForm(forms.Form):
    q = forms.CharField(label="الاسم أو رقم الهوية", required=False)
    month_from = MonthField(label="من شهر", required=False)
    month_to = MonthField(label="إلى شهر", required=False)
    date_from = forms.DateField(label="من تاريخ", required=False, widget=DATE_WIDGET)
    date_to = forms.DateField(label="إلى تاريخ", required=False, widget=DATE_WIDGET)
    aid_type = forms.ModelChoiceField(AidType.objects.all(), label="نوع المساعدة", required=False, empty_label="الكل")
    project = forms.ModelChoiceField(Project.objects.all(), label="المشروع", required=False, empty_label="الكل")
    status = forms.ChoiceField(
        label="الحالة", required=False,
        choices=[("", "الكل"), ("with", "استفاد (ضمن الفلاتر)"), ("without", "لم يستفد (ضمن الفلاتر)")],
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        _limit_projects(self, user)

    def clean(self):
        cd = super().clean()
        start, end = cd.get("date_from"), cd.get("date_to")
        if start and end and start > end:
            raise forms.ValidationError("تاريخ البداية بعد تاريخ النهاية")
        mf, mt = cd.get("month_from"), cd.get("month_to")
        if mf and mt and mf > mt:
            raise forms.ValidationError("شهر البداية بعد شهر النهاية")
        return cd


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            if not data:
                return [single(None, initial)]  # يرفع خطأ «مطلوب»
            return [single(d, initial) for d in data]
        return [single(data, initial)]


class UploadForm(forms.Form):
    kind = forms.ChoiceField(
        label="نوع الملف", initial="distribution", widget=forms.RadioSelect,
        choices=[
            ("distribution", "كشف توزيع (فيه استفادات)"),
            ("registry", "القائمة الأساسية للمستفيدين (أسماء فقط، بدون استفادة)"),
        ],
    )
    project = forms.ModelChoiceField(Project.objects.all(), label="المشروع", required=False, empty_label="— اختر مشروعاً —")
    new_project = forms.CharField(label="أو اسم مشروع جديد", required=False, max_length=200)
    file = MultipleFileField(label="ملفات إكسل (xlsx / xls / csv) — يمكن اختيار عدة ملفات")
    default_aid_type = forms.ModelChoiceField(
        AidType.objects.all(), label="نوع المساعدة (خارج الملف)", required=False,
        empty_label="— يؤخذ من عمود داخل الملف —",
    )
    new_aid_type = forms.CharField(label="أو نوع مساعدة جديد", required=False, max_length=100)
    default_date = forms.DateField(label="تاريخ التوزيع (خارج الملف)", required=False, widget=DATE_WIDGET)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        _limit_projects(self, user)

    def clean_file(self):
        files = self.cleaned_data["file"]
        for f in files:
            if not f.name.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv")):
                raise forms.ValidationError(f"«{f.name}»: الصيغة غير مدعومة. ارفع xlsx أو xls أو csv.")
            if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
                raise forms.ValidationError(f"«{f.name}»: الحجم يتجاوز {settings.MAX_UPLOAD_MB} ميغابايت.")
        return files

    def clean(self):
        cd = super().clean()
        if not cd.get("project") and not clean_text(cd.get("new_project")):
            raise forms.ValidationError("اختر مشروعاً أو اكتب اسم مشروع جديد.")
        if clean_text(cd.get("new_project")) and not cd.get("project"):
            from .roles import is_global
            if self.user is not None and not is_global(self.user):
                raise forms.ValidationError("إنشاء المشاريع من صلاحيات مدير النظام؛ اختر مشروعاً مسنداً إليك.")
        if cd.get("default_date") and cd["default_date"] > dt.date.today():
            self.add_error("default_date", "التاريخ في المستقبل")
        return cd


class MappingBase(forms.Form):
    default_aid_type = forms.ModelChoiceField(
        AidType.objects.all(), label="نوع المساعدة (خارج الملف)", required=False,
        empty_label="— يؤخذ من عمود داخل الملف —",
    )
    new_aid_type = forms.CharField(label="أو نوع مساعدة جديد", required=False, max_length=100)
    default_date = forms.DateField(label="تاريخ التوزيع (خارج الملف)", required=False, widget=DATE_WIDGET)


def make_mapping_form(columns, kind="distribution"):
    choices = [("", "— غير موجود في الملف —")] + [(c, c) for c in columns]
    registry = kind == "registry"
    fields = REGISTRY_FIELDS if registry else FIELDS
    required = REGISTRY_REQUIRED if registry else REQUIRED_FIELDS
    attrs = {
        f: forms.ChoiceField(choices=choices, required=f in required, label=FIELD_LABELS[f])
        for f in fields
    }
    return type("MappingForm", (MappingBase,), attrs)


class BeneficiaryForm(forms.ModelForm):
    """الحقول الإلزامية: الهوية، الاسم، اسم الزوجة رباعي، هوية الزوجة، عدد أفراد الأسرة (+ المشروع). الملاحظات اختيارية."""
    project = forms.ModelChoiceField(Project.objects.none(), label="المشروع", empty_label="— اختر المشروع —")

    class Meta:
        model = Beneficiary
        fields = ["national_id", "full_name", "wife_name", "wife_national_id", "family_size", "manager_notes",
                  "social_status"]
        widgets = {"social_status": forms.Textarea(attrs={"rows": 2}), "manager_notes": forms.Textarea(attrs={"rows": 3})}
        error_messages = {"national_id": {"unique": "يوجد مستفيد مسجل بنفس رقم الهوية."}}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("national_id", "full_name", "wife_name", "wife_national_id", "family_size"):
            self.fields[name].required = True
        self.fields["family_size"].min_value = 1
        self.fields["family_size"].widget.attrs["min"] = 1
        projects = visible_projects(user) if user is not None else Project.objects.all()
        self.fields["project"].queryset = projects
        if self.instance.pk:
            self.fields["project"].required = False       # المشروع يُحدَّد عند الإنشاء فقط
            del self.fields["project"]
        elif projects.count() == 1:
            self.fields["project"].initial = projects.first()

    def clean_national_id(self):
        nid = clean_id(self.cleaned_data["national_id"])
        if not is_valid_id(nid, settings.NATIONAL_ID_LENGTHS):
            lengths = " أو ".join(str(n) for n in settings.NATIONAL_ID_LENGTHS)
            raise forms.ValidationError(f"رقم الهوية يجب أن يتكون من {lengths} أرقام.")
        return nid

    def clean_full_name(self):
        name = clean_text(self.cleaned_data["full_name"])
        if not name:
            raise forms.ValidationError("الاسم مطلوب.")
        return name

    def clean_wife_name(self):
        name = clean_text(self.cleaned_data.get("wife_name"))
        if not name:
            raise forms.ValidationError("اسم الزوجة مطلوب.")
        if settings.STRICT_WIFE_QUAD_NAME and len(name.split()) < 4:
            raise forms.ValidationError("اسم الزوجة يجب أن يكون رباعياً (أربعة أسماء).")
        return name

    def clean_wife_national_id(self):
        nid = clean_id(self.cleaned_data.get("wife_national_id"))
        if not is_valid_id(nid, settings.NATIONAL_ID_LENGTHS):
            lengths = " أو ".join(str(n) for n in settings.NATIONAL_ID_LENGTHS)
            raise forms.ValidationError(f"هوية الزوجة يجب أن تتكون من {lengths} أرقام.")
        return nid

    def clean_family_size(self):
        n = self.cleaned_data.get("family_size")
        if not n or n < 1:
            raise forms.ValidationError("عدد أفراد الأسرة يجب أن يكون 1 أو أكثر.")
        return n

    def clean_manager_notes(self):
        return (self.cleaned_data.get("manager_notes") or "").strip()

    def clean(self):
        cd = super().clean()
        if cd.get("national_id") and cd.get("national_id") == cd.get("wife_national_id"):
            self.add_error("wife_national_id", "هوية الزوجة مطابقة لهوية المستفيد.")
        return cd


class BenefitForm(forms.Form):
    project = forms.ModelChoiceField(Project.objects.all(), label="المشروع", empty_label="— اختر —")
    aid_type = forms.ModelChoiceField(AidType.objects.all(), label="نوع المساعدة", empty_label="— اختر —")
    receipt_date = forms.DateField(label="تاريخ الاستلام", widget=DATE_WIDGET)
    note = forms.CharField(label="ملاحظة (اختياري)", required=False, max_length=300)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        _limit_projects(self, user)

    def clean_receipt_date(self):
        d = self.cleaned_data.get("receipt_date")
        if d and d > dt.date.today():
            raise forms.ValidationError("تاريخ الاستلام في المستقبل.")
        return d


class OptionalBenefitForm(BenefitForm):
    """للإدراج مع المستفيد الجديد: إمّا تُترك الحقول كلها فارغة أو تُملأ كلها."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            f.required = False

    def clean(self):
        cd = super().clean()
        filled = [bool(cd.get(k)) for k in ("aid_type", "receipt_date")]
        if any(filled) and not all(filled):
            raise forms.ValidationError("لإضافة استفادة املأ النوع والتاريخ معاً (المشروع اختياري)، أو اتركها فارغة.")
        return cd

    def has_benefit(self):
        return bool(self.cleaned_data.get("aid_type"))


class CompareForm(FilterForm):
    """فلاتر المقارنة (الفترة ونوع المساعدة والمشروع) دون البحث النصي والحالة."""
    file = forms.FileField(label="ملف إكسل للمقارنة (xlsx / xls / csv)", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields["q"]
        del self.fields["status"]

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f:
            if not f.name.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv")):
                raise forms.ValidationError("الصيغة غير مدعومة. ارفع xlsx أو xls أو csv.")
            if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
                raise forms.ValidationError(f"الحجم يتجاوز {settings.MAX_UPLOAD_MB} ميغابايت.")
        return f


class KashfChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, d):
        return f"{d.project} — {d.aid_type} — {d.distribution_date:%Y-%m-%d} ({d.names_count} اسم)"


class BenefitToKashfForm(forms.Form):
    """إضافة مستفيد موجود إلى كشف أو عدة كشوف قائمة."""
    distributions = KashfChoiceField(
        Distribution.objects.none(), label="الكشوف (اختر واحداً أو أكثر)",
        widget=forms.SelectMultiple(attrs={"size": 6}),
    )
    note = forms.CharField(label="ملاحظة (اختياري)", required=False, max_length=300)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # القائمة المعروضة آخر 400 كشف؛ والتحقق على كل الكشوف (ضمن مشاريع المستخدم فقط)
        base = Distribution.objects.select_related("project", "aid_type").order_by("-distribution_date", "-id")
        if user is not None:
            base = base.filter(project__in=visible_projects(user))
        self.fields["distributions"].queryset = base
        self.fields["distributions"].choices = [
            (d.pk, self.fields["distributions"].label_from_instance(d)) for d in base[:400]
        ]


class ProjectMembersForm(forms.Form):
    members = forms.ModelMultipleChoiceField(
        queryset=None, required=False, label="المستخدمون المسندون (مديرو الدوائر والموظفون)",
        widget=forms.SelectMultiple(attrs={"size": 12}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.contrib.auth import get_user_model
        self.fields["members"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")
        self.fields["members"].label_from_instance = lambda u: f"{u.get_full_name() or u.username} ({u.username})"


class UserForm(forms.Form):
    """إنشاء/تعديل مستخدم: الدور والمشاريع المسندة (مدير النظام فقط)."""
    username = forms.CharField(label="اسم المستخدم", max_length=150)
    full_name = forms.CharField(label="الاسم الكامل", max_length=150, required=False)
    password = forms.CharField(label="كلمة المرور", required=False, widget=forms.PasswordInput(render_value=False))
    role = forms.ChoiceField(label="الدور")
    projects = forms.ModelMultipleChoiceField(
        queryset=Project.objects.all(), required=False, label="المشاريع المسندة",
        widget=forms.SelectMultiple(attrs={"size": 8}),
    )
    is_active = forms.BooleanField(label="الحساب فعّال", required=False, initial=True)

    def __init__(self, *args, instance=None, lock_self=False, **kwargs):
        super().__init__(*args, **kwargs)
        from .roles import ROLE_CHOICES
        self.instance = instance
        self.fields["role"].choices = ROLE_CHOICES
        if instance is None:
            self.fields["password"].required = True
        if lock_self:                       # لا يغيّر مدير النظام دور حسابه أو يعطّله بنفسه
            del self.fields["role"]
            del self.fields["is_active"]

    def clean_username(self):
        from django.contrib.auth import get_user_model
        name = self.cleaned_data["username"].strip()
        qs = get_user_model().objects.filter(username__iexact=name)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("اسم المستخدم مستخدم مسبقاً.")
        return name

    def clean_password(self):
        pw = self.cleaned_data.get("password") or ""
        if pw:
            from django.contrib.auth.password_validation import validate_password
            validate_password(pw)
        return pw
