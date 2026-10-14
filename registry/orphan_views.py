import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import orphan_import, orphan_services as osv, services
from .exports import xlsx_response
from .models import Orphan, OrphanImport, Sponsor, Sponsorship, SponsorshipPayment
from .orphan_forms import (OrphanFilterForm, OrphanForm, OrphanUploadForm, PaymentForm, SponsorForm,
                           SponsorSearchForm, SponsorshipForm)
from .roles import ADMIN, ORPHAN_ROLES, role_required
from .textutils import clean_id, normalize_arabic

OR = ORPHAN_ROLES


def _page(request, qs, per_page=50):
    return Paginator(qs, per_page).get_page(request.GET.get("page"))


def _qs(request):
    p = request.GET.copy()
    p.pop("page", None)
    return p.urlencode()


# ============================================================ الأيتام
@role_required(*OR)
def orphan_list(request):
    form = OrphanFilterForm(request.GET or None)
    ctx = {"form": form, "searched": bool(request.GET)}
    if request.GET and form.is_valid():
        page = _page(request, osv.search_orphans(form.cleaned_data))
        page.object_list = osv.attach_sponsors(page.object_list)
        ctx.update(page=page, qs=_qs(request))
    elif not request.GET:
        ctx["s"] = osv.stats()
    return render(request, "registry/orphans/list.html", ctx)


@role_required(*OR)
def orphan_export(request):
    form = OrphanFilterForm(request.GET or None)
    if not form.is_valid():
        raise Http404
    qs = osv.search_orphans(form.cleaned_data)

    def rows():
        for i, o in enumerate(_iter_orphans(qs), start=1):
            f = o.family
            yield (i, o.full_name, o.national_id, o.birth_date.isoformat() if o.birth_date else "", o.age if o.age is not None else "",
                   o.gender, o.get_orphan_type_display(), f.governorate, f.area, f.mother_name, f.mother_national_id,
                   f.guardian_name, f.guardian_national_id, f.guardian_role, f.phone,
                   "، ".join(s.name for s in o.current_sponsors), o.education_status, o.health_status, o.notes)
    services.log(request.user, "export_orphans", urlencode(request.GET))
    return xlsx_response(
        "الأيتام.xlsx", "الأيتام",
        ["#", "اسم اليتيم", "رقم الهوية", "تاريخ الميلاد", "العمر", "الجنس", "حالة اليتم", "المحافظة", "المنطقة",
         "اسم الأم", "هوية الأم", "اسم الوصي", "هوية الوصي", "صفة الوصي", "الجوال", "الكفيل الحالي",
         "الحالة التعليمية", "الحالة الصحية", "ملاحظات"],
        rows(),
    )


def _iter_orphans(qs, chunk=1000):
    batch = []
    for o in qs.iterator(chunk_size=chunk):
        batch.append(o)
        if len(batch) >= chunk:
            yield from osv.attach_sponsors(batch)
            batch = []
    if batch:
        yield from osv.attach_sponsors(batch)


@role_required(*OR)
def orphan_detail(request, pk):
    o = get_object_or_404(Orphan.objects.select_related("family"), pk=pk, is_active=True)
    services.log(request.user, "orphan_view", o)
    siblings = o.family.orphans.filter(is_active=True).exclude(pk=o.pk)
    sps = o.sponsorships.select_related("sponsor").annotate(paid=Sum("payments__amount"), last=Max("payments__paid_on"))
    return render(request, "registry/orphans/detail.html", {
        "o": o, "siblings": siblings, "sponsorships": sps, "links": osv.family_links(o.family),
        "payment_form": PaymentForm(),
    })


@role_required(*OR)
def orphan_create(request):
    form = OrphanForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        o = form.save()
        services.log(request.user, "orphan_create", o)
        messages.success(request, "تم حفظ اليتيم.")
        return redirect("orphan_detail", pk=o.pk)
    return render(request, "registry/orphans/form.html", {"form": form, "title": "إدراج يتيم"})


@role_required(*OR)
def orphan_edit(request, pk):
    o = get_object_or_404(Orphan, pk=pk, is_active=True)
    form = OrphanForm(request.POST or None, instance=o)
    if request.method == "POST" and form.is_valid():
        form.save()
        services.log(request.user, "orphan_edit", o)
        messages.success(request, "تم حفظ التعديلات.")
        return redirect("orphan_detail", pk=o.pk)
    return render(request, "registry/orphans/form.html", {"form": form, "title": f"تعديل: {o.full_name}", "o": o})


@require_POST
@role_required(ADMIN)
def orphan_delete(request, pk):
    o = get_object_or_404(Orphan, pk=pk, is_active=True)
    o.is_active = False
    o.save()
    o.sponsorships.filter(status=Sponsorship.ACTIVE).update(status=Sponsorship.ENDED)
    services.log(request.user, "orphan_delete", o)
    messages.success(request, "تم إخفاء السجل.")
    return redirect("orphan_list")


# ============================================================ الرفع من إكسل
@role_required(*OR)
def orphan_import_new(request):
    form = OrphanUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        f = form.cleaned_data["file"]
        imp = OrphanImport(token=uuid.uuid4().hex, file_name=f.name, uploaded_by=request.user)
        imp.file.save(f.name, f, save=False)
        imp.save()
        return redirect("orphan_import_preview", token=imp.token)
    return render(request, "registry/orphans/import_new.html", {
        "form": form, "imports": OrphanImport.objects.all()[:15]})


@role_required(*OR)
def orphan_import_preview(request, token):
    imp = get_object_or_404(OrphanImport, token=token)
    if imp.status != OrphanImport.DRAFT:
        return render(request, "registry/orphans/import_preview.html", {"imp": imp, "done": True})
    try:
        parsed = orphan_import.parse_file(imp.file.path)
    except Exception as exc:
        return render(request, "registry/orphans/import_preview.html", {"imp": imp, "error": str(exc)})
    ctx = {
        "imp": imp, "stats": orphan_import.preview(parsed), "rejected": parsed["rejected"][:100],
        "warnings": parsed["warnings"][:100], "duplicates": parsed["duplicates"][:100],
        "sample": parsed["orphans"][:8],
    }
    return render(request, "registry/orphans/import_preview.html", ctx)


@require_POST
@role_required(*OR)
def orphan_import_confirm(request, token):
    imp = get_object_or_404(OrphanImport, token=token, status=OrphanImport.DRAFT)
    parsed = orphan_import.parse_file(imp.file.path)
    summary = orphan_import.commit(imp, parsed, request.user)
    messages.success(request, f"تم اعتماد الملف: {summary['created']} يتيم جديد، وتخطي {summary['skipped_existing']} موجود مسبقاً.")
    return redirect("orphan_list")


@require_POST
@role_required(ADMIN)
def orphan_import_cancel(request, token):
    imp = get_object_or_404(OrphanImport, token=token)
    if imp.status == OrphanImport.CONFIRMED:
        kept = orphan_import.cancel(imp, request.user)
        messages.success(request, "أُلغيت الرفعة وحُذف أيتامها" + (f" (بقي {kept} لوجود كفالة)." if kept else "."))
    elif imp.status == OrphanImport.DRAFT:
        imp.status = OrphanImport.CANCELLED
        imp.save()
    return redirect("orphan_import_new")


# ============================================================ الكفلاء
@role_required(*OR)
def sponsor_list(request):
    form = SponsorSearchForm(request.GET or None)
    qs = Sponsor.objects.order_by("name_search", "id").filter(is_active=True)
    q = (request.GET.get("q") or "").strip()
    if q:
        digits = clean_id(q)
        cond = Q(phone__contains=digits) if digits.isdigit() else Q()
        for tok in normalize_arabic(q).split():
            cond |= Q(name_search__contains=tok) | Q(organization__contains=tok)
        qs = qs.filter(cond)
    qs = qs.annotate(
        orphans_n=Count("sponsorships", filter=Q(sponsorships__status=Sponsorship.ACTIVE), distinct=True),
    )
    return render(request, "registry/orphans/sponsors.html", {"form": form, "page": _page(request, qs), "qs": _qs(request)})


@role_required(*OR)
def sponsor_create(request):
    form = SponsorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        s = form.save()
        services.log(request.user, "sponsor_create", s)
        messages.success(request, "تم حفظ الكفيل.")
        return redirect("sponsor_detail", pk=s.pk)
    return render(request, "registry/orphans/sponsor_form.html", {"form": form, "title": "إدراج كفيل"})


@role_required(*OR)
def sponsor_edit(request, pk):
    s = get_object_or_404(Sponsor, pk=pk, is_active=True)
    form = SponsorForm(request.POST or None, instance=s)
    if request.method == "POST" and form.is_valid():
        form.save()
        services.log(request.user, "sponsor_edit", s)
        messages.success(request, "تم حفظ التعديلات.")
        return redirect("sponsor_detail", pk=s.pk)
    return render(request, "registry/orphans/sponsor_form.html", {"form": form, "title": f"تعديل: {s.name}", "s": s})


@role_required(*OR)
def sponsor_detail(request, pk):
    s = get_object_or_404(Sponsor, pk=pk, is_active=True)
    sps = osv.sponsor_summary(s)
    overdue = {r["s"].pk for r in osv.overdue_sponsorships() if r["s"].sponsor_id == s.pk}
    return render(request, "registry/orphans/sponsor_detail.html", {
        "s": s, "sponsorships": sps, "overdue": overdue, "payment_form": PaymentForm(),
        "active_n": sum(1 for x in sps if x.status == Sponsorship.ACTIVE),
    })


@role_required(*OR)
def sponsor_export(request):
    rows = []
    for i, s in enumerate(Sponsor.objects.filter(is_active=True), start=1):
        sps = s.sponsorships.all()
        paid = SponsorshipPayment.objects.filter(sponsorship__sponsor=s).aggregate(t=Sum("amount"))["t"] or 0
        rows.append((i, s.name, s.organization, s.country, s.phone, s.email,
                     sps.filter(status=Sponsorship.ACTIVE).count(), sps.count(), paid))
    services.log(request.user, "export_sponsors")
    return xlsx_response("الكفلاء.xlsx", "الكفلاء",
                         ["#", "الكفيل", "الجهة", "البلد", "الجوال", "البريد", "كفالات نشطة", "إجمالي الكفالات", "إجمالي المدفوع"], rows)


# ============================================================ الكفالات والدفعات
@role_required(*OR)
def sponsorship_create(request, orphan_pk=None, sponsor_pk=None):
    orphan = get_object_or_404(Orphan, pk=orphan_pk, is_active=True) if orphan_pk else None
    sponsor = get_object_or_404(Sponsor, pk=sponsor_pk, is_active=True) if sponsor_pk else None
    form = SponsorshipForm(request.POST or None, initial={"sponsor": sponsor})
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        target = orphan
        if target is None:
            nid = clean_id(cd.get("orphan_id"))
            target = Orphan.objects.filter(national_id=nid, is_active=True).first() if nid else None
            if target is None:
                form.add_error("orphan_id", "لا يوجد يتيم بهذه الهوية. (يتيم بلا هوية: افتح صفحته وأضف الكفالة منها.)")
        if target is not None:
            sp, err = osv.create_sponsorship(
                cd["sponsor"], target, request.user, amount=cd.get("amount"), currency=cd["currency"],
                frequency=cd["frequency"], start_date=cd["start_date"], notes=cd.get("notes", ""))
            if err:
                form.add_error(None, err)
            else:
                messages.success(request, "تمت إضافة الكفالة.")
                return redirect("orphan_detail", pk=target.pk) if orphan else redirect("sponsor_detail", pk=cd["sponsor"].pk)
    return render(request, "registry/orphans/sponsorship_form.html", {"form": form, "orphan": orphan, "sponsor": sponsor})


@require_POST
@role_required(*OR)
def sponsorship_status(request, pk):
    sp = get_object_or_404(Sponsorship.objects.select_related("orphan", "sponsor"), pk=pk)
    status = request.POST.get("status")
    if status not in dict(Sponsorship.STATUS_CHOICES):
        raise Http404
    if osv.change_status(sp, status, user=request.user):
        messages.success(request, "تم تحديث حالة الكفالة.")
    else:
        messages.error(request, "اليتيم مكفول حالياً بكفالة نشطة أخرى.")
    if request.POST.get("back") == "sponsor":
        return redirect("sponsor_detail", pk=sp.sponsor_id)
    return redirect("orphan_detail", pk=sp.orphan_id)


@require_POST
@role_required(*OR)
def payment_add(request, pk):
    sp = get_object_or_404(Sponsorship.objects.select_related("orphan", "sponsor"), pk=pk)
    form = PaymentForm(request.POST)
    if form.is_valid():
        cd = form.cleaned_data
        osv.add_payment(sp, cd["paid_on"], cd["amount"], cd.get("note", ""), request.user)
        messages.success(request, "تم تسجيل الدفعة.")
    else:
        messages.error(request, "الدفعة غير صالحة: " + " ".join(sum(form.errors.values(), [])))
    return redirect("sponsor_detail", pk=sp.sponsor_id) if request.POST.get("back") == "sponsor" else redirect("orphan_detail", pk=sp.orphan_id)


@require_POST
@role_required(ADMIN)
def payment_delete(request, pk):
    p = get_object_or_404(SponsorshipPayment.objects.select_related("sponsorship__orphan"), pk=pk)
    who = p.sponsorship.orphan_id
    services.log(request.user, "sponsorship_payment_delete", p.sponsorship.orphan, f"{p.amount} في {p.paid_on}")
    p.delete()
    messages.success(request, "حُذفت الدفعة.")
    return redirect("orphan_detail", pk=who)


# ============================================================ إحصاءات الكفالة
@role_required(*OR)
def orphan_stats(request):
    return render(request, "registry/orphans/stats.html", {"s": osv.stats()})
