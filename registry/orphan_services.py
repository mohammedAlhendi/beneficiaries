"""خدمات الأيتام والكفلاء: بحث، كفالات، دفعات، تأخر، إحصاءات."""
import datetime as dt
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max, Q, Sum

from .models import Beneficiary, Orphan, OrphanFamily, Sponsor, Sponsorship, SponsorshipPayment
from .services import log
from .textutils import clean_id, normalize_arabic

PERIOD_DAYS = {"monthly": 31, "quarterly": 92, "yearly": 366}
GRACE_DAYS = 10
AGE_GROUPS = [("أقل من 5", 0, 4), ("5 - 9", 5, 9), ("10 - 14", 10, 14), ("15 - 17", 15, 17), ("18 فأكثر", 18, 200)]


def _birth_bounds(age_from, age_to, today=None):
    today = today or dt.date.today()
    lo = hi = None
    if age_from is not None:   # عمره >= age_from  ⇒ وُلد قبل أو في هذا التاريخ
        try:
            hi = today.replace(year=today.year - age_from)
        except ValueError:
            hi = today.replace(year=today.year - age_from, day=28)
    if age_to is not None:     # عمره <= age_to ⇒ وُلد بعد هذا التاريخ
        try:
            lo = today.replace(year=today.year - age_to - 1) + dt.timedelta(days=1)
        except ValueError:
            lo = today.replace(year=today.year - age_to - 1, day=28) + dt.timedelta(days=1)
    return lo, hi


def search_orphans(cd):
    qs = Orphan.objects.filter(is_active=True).select_related("family")
    text = (cd.get("q") or "").strip()
    if text:
        digits = clean_id(text)
        if digits.isdigit():
            qs = qs.filter(
                Q(national_id__contains=digits) | Q(family__mother_national_id__contains=digits)
                | Q(family__guardian_national_id__contains=digits) | Q(family__phone__contains=digits)
            )
        else:
            for tok in normalize_arabic(text).split():
                qs = qs.filter(Q(name_search__contains=tok) | Q(family__names_search__contains=tok))
    if cd.get("governorate"):
        qs = qs.filter(family__governorate=cd["governorate"])
    if cd.get("area"):
        qs = qs.filter(family__area__contains=cd["area"])
    if cd.get("gender"):
        qs = qs.filter(gender=cd["gender"])
    lo, hi = _birth_bounds(cd.get("age_from"), cd.get("age_to"))
    if lo:
        qs = qs.filter(birth_date__gte=lo)
    if hi:
        qs = qs.filter(birth_date__lte=hi)
    if cd.get("sponsor"):
        qs = qs.filter(sponsorships__sponsor=cd["sponsor"], sponsorships__status=Sponsorship.ACTIVE)
    st = cd.get("sponsored")
    active = Sponsorship.objects.filter(status=Sponsorship.ACTIVE).values("orphan_id")
    if st == "yes":
        qs = qs.filter(pk__in=active)
    elif st == "no":
        qs = qs.exclude(pk__in=active)
    return qs.distinct().order_by("name_search", "id")


def attach_sponsors(items):
    items = list(items)
    ids = [o.pk for o in items]
    m = {}
    for s in Sponsorship.objects.filter(orphan_id__in=ids, status=Sponsorship.ACTIVE).select_related("sponsor"):
        m.setdefault(s.orphan_id, []).append(s.sponsor)
    for o in items:
        o.current_sponsors = m.get(o.pk, [])
    return items


def family_links(family):
    """هل أم اليتيم أو وصيّه مسجلان في قاعدة المستفيدين العامة؟ (بالهوية)"""
    ids = [i for i in (family.mother_national_id, family.guardian_national_id) if i]
    if not ids:
        return []
    return list(Beneficiary.objects.filter(national_id__in=ids, is_active=True))


# ------------------------------------------------------------------ الكفالات
@transaction.atomic
def create_sponsorship(sponsor, orphan, user=None, **fields):
    """كفالة جديدة. يعيد (sponsorship|None, error|None): لا كفالتان نشطتان لنفس اليتيم."""
    if fields.get("status", Sponsorship.ACTIVE) == Sponsorship.ACTIVE:
        cur = Sponsorship.objects.filter(orphan=orphan, status=Sponsorship.ACTIVE).select_related("sponsor").first()
        if cur:
            return None, f"اليتيم مكفول حالياً لدى «{cur.sponsor.name}». أنهِ تلك الكفالة أولاً."
    sp = Sponsorship.objects.create(sponsor=sponsor, orphan=orphan, created_by=user, **fields)
    log(user, "sponsorship_create", orphan, f"الكفيل: {sponsor.name}")
    return sp, None


@transaction.atomic
def change_status(sp, status, end_date=None, user=None):
    if status == Sponsorship.ACTIVE:
        clash = Sponsorship.objects.filter(orphan=sp.orphan, status=Sponsorship.ACTIVE).exclude(pk=sp.pk).exists()
        if clash:
            return False
    sp.status = status
    if status == Sponsorship.ENDED:
        sp.end_date = end_date or dt.date.today()
    elif status == Sponsorship.ACTIVE:
        sp.end_date = None
    sp.save()
    log(user, "sponsorship_status", sp.orphan, f"{sp.sponsor.name} → {sp.get_status_display()}")
    return True


def add_payment(sp, paid_on, amount, note="", user=None):
    p = SponsorshipPayment.objects.create(sponsorship=sp, paid_on=paid_on, amount=amount, note=note, created_by=user)
    log(user, "sponsorship_payment", sp.orphan, f"{sp.sponsor.name}: {amount} {sp.currency} في {paid_on}")
    return p


def overdue_sponsorships(today=None):
    """كفالات نشطة دورية تأخرت دفعتها: آخر دفعة (أو البداية) أقدم من الدورية + مهلة."""
    today = today or dt.date.today()
    rows = []
    qs = (
        Sponsorship.objects.filter(status=Sponsorship.ACTIVE).exclude(frequency="once")
        .select_related("sponsor", "orphan").annotate(last=Max("payments__paid_on"))
    )
    for s in qs:
        ref = s.last or s.start_date
        due = ref + dt.timedelta(days=PERIOD_DAYS[s.frequency] + GRACE_DAYS)
        if today > due:
            rows.append({"s": s, "last": s.last, "days": (today - ref).days})
    rows.sort(key=lambda r: -r["days"])
    return rows


def sponsor_summary(sponsor):
    sps = sponsor.sponsorships.select_related("orphan__family").annotate(
        paid=Sum("payments__amount"), last=Max("payments__paid_on"), n_pay=Count("payments")
    )
    return list(sps)


def stats(today=None):
    today = today or dt.date.today()
    active_orphans = Orphan.objects.filter(is_active=True)
    total = active_orphans.count()
    sponsored = active_orphans.filter(sponsorships__status=Sponsorship.ACTIVE).distinct().count()
    by_gov = list(active_orphans.values("family__governorate").annotate(n=Count("id")).order_by("-n"))
    by_gender = list(active_orphans.values("gender").annotate(n=Count("id")).order_by("-n"))
    groups = []
    for label, a, b in AGE_GROUPS:
        lo, hi = _birth_bounds(a, b, today)
        groups.append((label, active_orphans.filter(birth_date__gte=lo, birth_date__lte=hi).count()))
    paid = SponsorshipPayment.objects.values("sponsorship__currency").annotate(t=Sum("amount")).order_by()
    return {
        "total": total, "sponsored": sponsored, "unsponsored": total - sponsored,
        "families": OrphanFamily.objects.count(), "sponsors": Sponsor.objects.filter(is_active=True).count(),
        "no_birth": active_orphans.filter(birth_date__isnull=True).count(),
        "no_id": active_orphans.filter(national_id="").count(),
        "by_gov": by_gov, "by_gender": by_gender, "age_groups": groups,
        "paid": list(paid), "overdue": overdue_sponsorships(today),
    }
