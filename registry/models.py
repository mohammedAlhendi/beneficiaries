from django.conf import settings
from django.db import models

from .textutils import normalize_arabic


class AidType(models.Model):
    name = models.CharField("نوع المساعدة", max_length=100, unique=True)

    class Meta:
        verbose_name = "نوع مساعدة"
        verbose_name_plural = "أنواع المساعدات"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Project(models.Model):
    name = models.CharField("اسم المشروع", max_length=200, unique=True)
    description = models.TextField("وصف", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="assigned_projects", verbose_name="المستخدمون المسندون")

    class Meta:
        verbose_name = "مشروع"
        verbose_name_plural = "المشاريع"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Upload(models.Model):
    DRAFT, CONFIRMED, CANCELLED = "draft", "confirmed", "cancelled"
    STATUS_CHOICES = [(DRAFT, "قيد المراجعة"), (CONFIRMED, "معتمد"), (CANCELLED, "ملغى")]
    DISTRIBUTION, REGISTRY = "distribution", "registry"
    KIND_CHOICES = [(DISTRIBUTION, "كشف توزيع"), (REGISTRY, "القائمة الأساسية للمستفيدين")]

    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="uploads", verbose_name="المشروع")
    file = models.FileField(upload_to="uploads/%Y/%m/")
    file_name = models.CharField(max_length=255)
    default_aid_type = models.ForeignKey(AidType, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    default_date = models.DateField(null=True, blank=True)
    mapping = models.JSONField(default=dict, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DRAFT)
    kind = models.CharField(max_length=12, choices=KIND_CHOICES, default=DISTRIBUTION)
    batch = models.CharField(max_length=36, blank=True, db_index=True)
    rows_total = models.PositiveIntegerField(default=0)
    rows_accepted = models.PositiveIntegerField(default=0)
    rows_rejected = models.PositiveIntegerField(default=0)
    rows_duplicate = models.PositiveIntegerField(default=0)
    report = models.JSONField("تقرير دمج الهويات", default=dict, blank=True)

    class Meta:
        verbose_name = "عملية رفع"
        verbose_name_plural = "عمليات الرفع"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.file_name} ({self.get_status_display()})"


class Beneficiary(models.Model):
    national_id = models.CharField("رقم الهوية", max_length=20, unique=True)
    full_name = models.CharField("الاسم", max_length=300)
    name_search = models.CharField(max_length=300, db_index=True, editable=False)
    social_status = models.TextField("تفاصيل الحالة الاجتماعية", blank=True)
    wife_name = models.CharField("اسم الزوجة رباعي", max_length=300, blank=True)
    wife_name_search = models.CharField(max_length=300, blank=True, editable=False)
    wife_national_id = models.CharField("رقم هوية الزوجة", max_length=20, blank=True, db_index=True)
    family_size = models.PositiveIntegerField("عدد أفراد الأسرة", null=True, blank=True)
    manager_notes = models.TextField("ملاحظات المدير", blank=True)
    home_project = models.ForeignKey(
        "Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="home_beneficiaries",
        verbose_name="المشروع الذي سُجّل فيه")
    is_active = models.BooleanField("فعّال", default=True)
    created_by_upload = models.ForeignKey(Upload, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_beneficiaries")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "مستفيد"
        verbose_name_plural = "المستفيدون"
        ordering = ["name_search", "id"]

    def save(self, *args, **kwargs):
        self.name_search = normalize_arabic(self.full_name)
        self.wife_name_search = normalize_arabic(self.wife_name)
        super().save(*args, **kwargs)

    @property
    def missing_required(self):
        """الحقول الإلزامية الناقصة (سجلات قديمة قبل التحديث)."""
        out = []
        if not self.wife_name:
            out.append("اسم الزوجة")
        if not self.wife_national_id:
            out.append("هوية الزوجة")
        if not self.family_size:
            out.append("عدد أفراد الأسرة")
        return out

    def __str__(self):
        return f"{self.full_name} ({self.national_id})"


class UploadRow(models.Model):
    """نسخة كاملة من كل صف في الملف المرفوع كما وصل (كل الأعمدة) دون فقدان أي معلومة."""
    upload = models.ForeignKey(Upload, on_delete=models.CASCADE, related_name="raw_rows")
    sheet = models.CharField(max_length=100, blank=True)
    row_number = models.PositiveIntegerField(null=True, blank=True)
    data = models.JSONField(default=dict)

    class Meta:
        verbose_name = "صف خام"
        verbose_name_plural = "الصفوف الخام"
        ordering = ["id"]


class Distribution(models.Model):
    """كشف توزيع: نوع مساعدة + تاريخ + مشروع (يُنشأ لكل مجموعة في الملف أو عند إدخال يدوي)."""
    upload = models.ForeignKey(Upload, null=True, blank=True, on_delete=models.SET_NULL, related_name="distributions")
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="distributions", verbose_name="المشروع")
    aid_type = models.ForeignKey(AidType, on_delete=models.PROTECT, related_name="distributions", verbose_name="نوع المساعدة")
    distribution_date = models.DateField("تاريخ التوزيع", db_index=True)
    names_count = models.PositiveIntegerField("عدد الأسماء", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "كشف"
        verbose_name_plural = "الكشوفات"
        ordering = ["-distribution_date", "-id"]

    def __str__(self):
        return f"{self.aid_type} - {self.distribution_date}"


class NameAlias(models.Model):
    """اسم بديل ورد في ملف لنفس رقم الهوية باسم مختلف (يُحفظ للمراجعة دون إنشاء مستفيد مكرر)."""
    beneficiary = models.ForeignKey("Beneficiary", on_delete=models.CASCADE, related_name="aliases")
    name = models.CharField("الاسم الوارد", max_length=300)
    name_search = models.CharField(max_length=300, editable=False)
    source = models.CharField("المصدر", max_length=255, blank=True)
    upload = models.ForeignKey(Upload, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "اسم بديل"
        verbose_name_plural = "الأسماء البديلة"
        ordering = ["-created_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["beneficiary", "name_search"], name="unique_alias_per_person")]

    def save(self, *args, **kwargs):
        self.name_search = normalize_arabic(self.name)
        super().save(*args, **kwargs)


class Benefit(models.Model):
    UPLOAD, MANUAL = "upload", "manual"
    SOURCE_CHOICES = [(UPLOAD, "ملف إكسل"), (MANUAL, "إدخال يدوي")]

    beneficiary = models.ForeignKey(Beneficiary, on_delete=models.CASCADE, related_name="benefits")
    distribution = models.ForeignKey(Distribution, on_delete=models.CASCADE, related_name="benefits")
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="benefits")
    aid_type = models.ForeignKey(AidType, on_delete=models.PROTECT, related_name="benefits")
    receipt_date = models.DateField(db_index=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=UPLOAD)
    note = models.CharField("ملاحظة", max_length=300, blank=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(null=True, blank=True, auto_now_add=True)

    @property
    def source_label(self):
        """اسم الملف المصدر للاستفادات المرفوعة، أو «إدخال يدوي»."""
        if self.source == self.MANUAL:
            return "إدخال يدوي"
        up = self.distribution.upload if self.distribution_id else None
        return up.file_name if up else "ملف إكسل (محذوف)"

    class Meta:
        verbose_name = "استفادة"
        verbose_name_plural = "الاستفادات"
        ordering = ["-receipt_date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["beneficiary", "project", "aid_type", "receipt_date"],
                name="unique_benefit_per_day",
            )
        ]


class AuditLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    username = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=50)
    object_repr = models.CharField(max_length=300, blank=True)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "سجل تدقيق"
        verbose_name_plural = "سجل التدقيق"
        ordering = ["-created_at", "-id"]



# ====================================================================== الأيتام والكفلاء
class OrphanImport(models.Model):
    """عملية رفع ملف أيتام (للتتبع والتراجع)."""
    DRAFT, CONFIRMED, CANCELLED = "draft", "confirmed", "cancelled"
    STATUS_CHOICES = [(DRAFT, "قيد المراجعة"), (CONFIRMED, "معتمد"), (CANCELLED, "ملغى")]
    token = models.CharField(max_length=32, unique=True)
    file = models.FileField(upload_to="orphans/%Y/%m/")
    file_name = models.CharField(max_length=255)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DRAFT)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "رفع ملف أيتام"
        verbose_name_plural = "رفوعات ملفات الأيتام"
        ordering = ["-uploaded_at"]

    def __str__(self):
        return self.file_name


class OrphanFamily(models.Model):
    """أسرة اليتيم: الأم والوصي والعنوان ووسيلة التواصل (قد تضم أكثر من يتيم)."""
    governorate = models.CharField("المحافظة", max_length=100, blank=True)
    area = models.CharField("المنطقة", max_length=200, blank=True)
    mother_name = models.CharField("اسم الأم", max_length=300, blank=True)
    mother_national_id = models.CharField("هوية الأم", max_length=20, blank=True, db_index=True)
    guardian_name = models.CharField("اسم الوصي", max_length=300, blank=True)
    guardian_national_id = models.CharField("هوية الوصي", max_length=20, blank=True)
    guardian_role = models.CharField("صفة الوصي", max_length=50, blank=True)
    phone = models.CharField("جوال (واتساب)", max_length=30, blank=True)
    names_search = models.CharField(max_length=700, blank=True, db_index=True, editable=False)
    import_batch = models.ForeignKey(OrphanImport, null=True, blank=True, on_delete=models.SET_NULL, related_name="families")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        self.names_search = normalize_arabic(f"{self.mother_name} {self.guardian_name}")
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = "أسرة أيتام"
        verbose_name_plural = "أسر الأيتام"

    def __str__(self):
        return self.guardian_name or self.mother_name or f"أسرة {self.pk}"


class Orphan(models.Model):
    MALE, FEMALE = "ذكر", "أنثى"
    GENDER_CHOICES = [(MALE, "ذكر"), (FEMALE, "أنثى")]
    ORPHAN_TYPES = [("father", "يتيم الأب"), ("mother", "يتيم الأم"), ("both", "يتيم الأبوين")]

    family = models.ForeignKey(OrphanFamily, on_delete=models.PROTECT, related_name="orphans")
    full_name = models.CharField("اسم اليتيم", max_length=300)
    name_search = models.CharField(max_length=300, db_index=True, editable=False)
    national_id = models.CharField("رقم الهوية", max_length=20, blank=True, db_index=True)
    birth_date = models.DateField("تاريخ الميلاد", null=True, blank=True)
    gender = models.CharField("الجنس", max_length=10, choices=GENDER_CHOICES, blank=True)
    orphan_type = models.CharField("حالة اليتم", max_length=10, choices=ORPHAN_TYPES, blank=True)
    education_status = models.CharField("الحالة التعليمية", max_length=200, blank=True)
    health_status = models.CharField("الحالة الصحية", max_length=300, blank=True)
    notes = models.TextField("ملاحظات", blank=True)
    is_active = models.BooleanField("فعّال", default=True)
    import_batch = models.ForeignKey(OrphanImport, null=True, blank=True, on_delete=models.SET_NULL, related_name="orphans")
    source_row = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "يتيم"
        verbose_name_plural = "الأيتام"
        ordering = ["name_search", "id"]
        constraints = [
            models.UniqueConstraint(fields=["national_id"], condition=~models.Q(national_id=""), name="unique_orphan_national_id"),
        ]

    def save(self, *args, **kwargs):
        self.name_search = normalize_arabic(self.full_name)
        super().save(*args, **kwargs)

    @property
    def age(self):
        if not self.birth_date:
            return None
        import datetime as dt
        t = dt.date.today()
        return t.year - self.birth_date.year - ((t.month, t.day) < (self.birth_date.month, self.birth_date.day))

    def __str__(self):
        return self.full_name


class Sponsor(models.Model):
    name = models.CharField("اسم الكفيل", max_length=300)
    name_search = models.CharField(max_length=300, db_index=True, editable=False)
    organization = models.CharField("الجهة", max_length=300, blank=True)
    country = models.CharField("البلد", max_length=100, blank=True)
    phone = models.CharField("الجوال", max_length=40, blank=True)
    email = models.CharField("البريد الإلكتروني", max_length=200, blank=True)
    notes = models.TextField("ملاحظات", blank=True)
    is_active = models.BooleanField("فعّال", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "كفيل"
        verbose_name_plural = "الكفلاء"
        ordering = ["name_search", "id"]

    def save(self, *args, **kwargs):
        self.name_search = normalize_arabic(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Sponsorship(models.Model):
    ACTIVE, SUSPENDED, ENDED = "active", "suspended", "ended"
    STATUS_CHOICES = [(ACTIVE, "نشطة"), (SUSPENDED, "موقوفة"), (ENDED, "منتهية")]
    FREQ_CHOICES = [("monthly", "شهرية"), ("quarterly", "ربع سنوية"), ("yearly", "سنوية"), ("once", "مرة واحدة")]

    sponsor = models.ForeignKey(Sponsor, on_delete=models.PROTECT, related_name="sponsorships")
    orphan = models.ForeignKey(Orphan, on_delete=models.CASCADE, related_name="sponsorships")
    amount = models.DecimalField("المبلغ", max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField("العملة", max_length=10, default="USD")
    frequency = models.CharField("الدورية", max_length=10, choices=FREQ_CHOICES, default="monthly")
    start_date = models.DateField("بداية الكفالة")
    end_date = models.DateField("نهاية الكفالة", null=True, blank=True)
    status = models.CharField("الحالة", max_length=10, choices=STATUS_CHOICES, default=ACTIVE)
    notes = models.TextField("ملاحظات", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "كفالة"
        verbose_name_plural = "الكفالات"
        ordering = ["-start_date", "-id"]


class SponsorshipPayment(models.Model):
    sponsorship = models.ForeignKey(Sponsorship, on_delete=models.CASCADE, related_name="payments")
    paid_on = models.DateField("تاريخ الدفعة")
    amount = models.DecimalField("المبلغ", max_digits=12, decimal_places=2)
    note = models.CharField("ملاحظة", max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "دفعة كفالة"
        verbose_name_plural = "دفعات الكفالات"
        ordering = ["-paid_on", "-id"]
