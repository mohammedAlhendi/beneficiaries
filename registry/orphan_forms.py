import datetime as dt

from django import forms
from django.conf import settings

from .forms import DATE_WIDGET, FilterForm
from .models import Orphan, OrphanFamily, Sponsor, Sponsorship, SponsorshipPayment
from .textutils import clean_id, clean_text, is_valid_id

FAMILY_FIELDS = ["governorate", "area", "mother_name", "mother_national_id",
                 "guardian_name", "guardian_national_id", "guardian_role", "phone"]


class OrphanFilterForm(forms.Form):
    q = forms.CharField(label="اسم اليتيم/الوصي أو الهوية أو الجوال", required=False)
    governorate = forms.ChoiceField(label="المحافظة", required=False)
    area = forms.CharField(label="المنطقة", required=False)
    gender = forms.ChoiceField(label="الجنس", required=False, choices=[("", "الكل"), ("ذكر", "ذكر"), ("أنثى", "أنثى")])
    age_from = forms.IntegerField(label="العمر من", required=False, min_value=0, max_value=120)
    age_to = forms.IntegerField(label="العمر إلى", required=False, min_value=0, max_value=120)
    sponsored = forms.ChoiceField(
        label="الكفالة", required=False, choices=[("", "الكل"), ("yes", "مكفول"), ("no", "غير مكفول")])
    sponsor = forms.ModelChoiceField(Sponsor.objects.all(), label="الكفيل", required=False, empty_label="الكل")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        govs = OrphanFamily.objects.exclude(governorate="").values_list("governorate", flat=True).distinct().order_by("governorate")
        self.fields["governorate"].choices = [("", "الكل")] + [(g, g) for g in govs]

    def clean(self):
        cd = super().clean()
        a, b = cd.get("age_from"), cd.get("age_to")
        if a is not None and b is not None and a > b:
            raise forms.ValidationError("العمر (من) أكبر من (إلى)")
        return cd


class OrphanForm(forms.ModelForm):
    governorate = forms.CharField(label="المحافظة", required=False, max_length=100)
    area = forms.CharField(label="المنطقة", required=False, max_length=200)
    mother_name = forms.CharField(label="اسم الأم", required=False, max_length=300)
    mother_national_id = forms.CharField(label="هوية الأم", required=False, max_length=20)
    guardian_name = forms.CharField(label="اسم الوصي (إن كان غير الأم)", required=False, max_length=300)
    guardian_national_id = forms.CharField(label="هوية الوصي", required=False, max_length=20)
    guardian_role = forms.CharField(label="صفة الوصي", required=False, max_length=50)
    phone = forms.CharField(label="جوال (واتساب)", required=False, max_length=30)

    class Meta:
        model = Orphan
        fields = ["full_name", "national_id", "birth_date", "gender", "orphan_type",
                  "education_status", "health_status", "notes"]
        widgets = {"birth_date": DATE_WIDGET, "notes": forms.Textarea(attrs={"rows": 3})}
        error_messages = {"national_id": {"unique": "يوجد يتيم مسجل بنفس رقم الهوية."}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["national_id"].required = False
        self.fields["gender"].required = False
        self.fields["orphan_type"].required = False
        self.fields["gender"].choices = [("", "—")] + list(Orphan.GENDER_CHOICES)
        self.fields["orphan_type"].choices = [("", "—")] + list(Orphan.ORPHAN_TYPES)
        if self.instance.pk:
            fam = self.instance.family
            for f in FAMILY_FIELDS:
                self.fields[f].initial = getattr(fam, f)

    def clean_national_id(self):
        nid = clean_id(self.cleaned_data.get("national_id"))
        if nid and not is_valid_id(nid, settings.NATIONAL_ID_LENGTHS):
            raise forms.ValidationError("رقم الهوية غير صالح (9 أرقام) أو اتركه فارغاً.")
        return nid

    def clean_full_name(self):
        n = clean_text(self.cleaned_data.get("full_name"))
        if not n:
            raise forms.ValidationError("الاسم مطلوب.")
        return n

    def clean_birth_date(self):
        d = self.cleaned_data.get("birth_date")
        if d and d > dt.date.today():
            raise forms.ValidationError("تاريخ الميلاد في المستقبل.")
        return d

    def save(self, commit=True):
        orphan = super().save(commit=False)
        data = {f: clean_text(self.cleaned_data.get(f)) for f in FAMILY_FIELDS}
        for f in ("mother_national_id", "guardian_national_id", "phone"):
            data[f] = clean_id(data[f])
        mid = data["mother_national_id"]
        fam = orphan.family if orphan.pk else None
        if fam is None:
            fam = OrphanFamily.objects.filter(mother_national_id=mid).first() if mid else None
        if fam is None:
            fam = OrphanFamily(**data)
        else:
            for k, v in data.items():
                setattr(fam, k, v)
        fam.save()
        orphan.family = fam
        if commit:
            orphan.save()
        return orphan


class SponsorForm(forms.ModelForm):
    class Meta:
        model = Sponsor
        fields = ["name", "organization", "country", "phone", "email", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class SponsorSearchForm(forms.Form):
    q = forms.CharField(label="اسم الكفيل أو الجهة أو الجوال", required=False)


class SponsorshipForm(forms.Form):
    sponsor = forms.ModelChoiceField(Sponsor.objects.filter(is_active=True), label="الكفيل", empty_label="— اختر —")
    orphan_id = forms.CharField(label="رقم هوية اليتيم", required=False)
    amount = forms.DecimalField(label="المبلغ", required=False, min_value=0, max_digits=12, decimal_places=2)
    currency = forms.CharField(label="العملة", initial="USD", max_length=10)
    frequency = forms.ChoiceField(label="الدورية", choices=Sponsorship.FREQ_CHOICES)
    start_date = forms.DateField(label="بداية الكفالة", widget=DATE_WIDGET, initial=dt.date.today)
    notes = forms.CharField(label="ملاحظات", required=False, widget=forms.Textarea(attrs={"rows": 2}))


class PaymentForm(forms.Form):
    paid_on = forms.DateField(label="تاريخ الدفعة", widget=DATE_WIDGET, initial=dt.date.today)
    amount = forms.DecimalField(label="المبلغ", min_value=0.01, max_digits=12, decimal_places=2)
    note = forms.CharField(label="ملاحظة", required=False, max_length=300)

    def clean_paid_on(self):
        d = self.cleaned_data["paid_on"]
        if d > dt.date.today():
            raise forms.ValidationError("تاريخ الدفعة في المستقبل.")
        return d


class OrphanUploadForm(forms.Form):
    file = forms.FileField(label="ملف إكسل للأيتام (xlsx / xls / csv)")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv")):
            raise forms.ValidationError("الصيغة غير مدعومة. ارفع xlsx أو xls أو csv.")
        if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(f"الحجم يتجاوز {settings.MAX_UPLOAD_MB} ميغابايت.")
        return f
