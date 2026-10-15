import os
import re
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .exports import xlsx_response
from .forms import (BenefitForm, BenefitToKashfForm, BeneficiaryForm, CompareForm, FilterForm, OptionalBenefitForm,
                    ProjectMembersForm, UploadForm, UserForm, make_mapping_form)
from .importer import (FIELDS, REGISTRY_REQUIRED, REQUIRED_FIELDS, auto_map, clean_rows, load_table,
                       table_columns)
from .models import AuditLog, Beneficiary, Benefit, Distribution, NameAlias, Project, Upload
from .roles import (ADMIN, ALL_ROLES, EDIT_ROLES, READ_ROLES, has_role, project_scope, role_required,
                    visible_projects)
from .services import scope_q
from .textutils import clean_text

ANY = READ_ROLES      # بحث واستعلام (موظف فما فوق)
EDIT = EDIT_ROLES     # إضافة وتعديل ورفع ملفات وإدارة بيانات (مدير الدائرة / مدير النظام)


def _scope(request):
    return project_scope(request.user)


def _ben(request, pk):
    """مستفيد ضمن نطاق مشاريع المستخدم، وإلا 404."""
    return get_object_or_404(services.visible_beneficiaries(_scope(request)), pk=pk)


def _uploads(request):
    return Upload.objects.filter(project__in=visible_projects(request.user))


def _dists(request):
    return Distribution.objects.filter(project__in=visible_projects(request.user))


def scoped_benefits(request):
    return Benefit.objects.filter(scope_q(_scope(request)))


def _page(request, qs, per_page=50):
    return Paginator(qs, per_page).get_page(request.GET.get("page"))


def _qs_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


def _filters(request, key="q"):
    """(نموذج الفلترة، قيم الفلترة + «scope» نطاق مشاريع المستخدم دائماً)."""
    form = FilterForm(request.GET or None, user=request.user)
    cd = {"scope": _scope(request)}
    if request.GET and form.is_valid():
        cd.update(form.cleaned_data)
    return form, cd


# ================================================================ البحث
@role_required(*ANY)
def search(request):
    form, cd = _filters(request)
    ctx = {"form": form, "searched": bool(request.GET)}
    if request.GET and form.is_valid():
        qs, filtered = services.search_beneficiaries(cd)
        page = _page(request, qs)
        page.object_list = services.annotate_beneficiaries(page.object_list, cd)
        ctx.update(page=page, filtered=filtered, qs=_qs_without_page(request), status=cd.get("status"))
        if cd.get("q"):
            services.log(request.user, "search", cd["q"])
    else:
        ctx.update(stats=services.home_stats(_scope(request)))
    return render(request, "registry/search.html", ctx)


@role_required(*ANY)
def search_suggest(request):
    """اقتراحات فورية أثناء الكتابة: بهوية الزوج أو الزوجة أو بالاسم."""
    return JsonResponse({"results": services.suggest(request.GET.get("q", ""), _scope(request))})


@role_required(*ANY)
def search_export(request):
    form, cd = _filters(request)
    if not form.is_valid():
        raise Http404
    rows = (
        (i, b.full_name, b.national_id, b.wife_name, b.wife_national_id, b.family_size or "", b.manager_notes,
         b.social_status, b.aid_types_text, b.total_benefits, b.matched_benefits,
         b.projects_count, b.last_date.isoformat() if b.last_date else "")
        for i, b in enumerate(services.iter_search_rows(cd), start=1)
    )
    services.log(request.user, "export_search", urlencode(request.GET))
    return xlsx_response(
        "نتائج-البحث.xlsx", "نتائج البحث",
        ["#", "الاسم", "رقم الهوية", "اسم الزوجة", "هوية الزوجة", "عدد الأفراد", "ملاحظات المدير", "الحالة الاجتماعية",
         "أنواع المساعدة", "عدد مرات الاستفادة", "ضمن الفلتر", "عدد المشاريع", "آخر استلام"],
        rows,
    )


# ================================================================ المستفيد
@role_required(*ANY)
def beneficiary_detail(request, pk):
    b = _ben(request, pk)
    form, cd = _filters(request)
    mine = b.benefits.filter(scope_q(cd["scope"]))          # استفادات ضمن مشاريع المستخدم فقط
    benefits = mine.filter(services.benefit_q(cd)).select_related("project", "aid_type", "distribution__upload")
    by_type = list(mine.values("aid_type__name").annotate(n=Count("id")).order_by("-n"))
    ctx = {
        "b": b, "form": form, "benefits": benefits, "by_type": by_type,
        "total": mine.count(),
        "projects_count": mine.values("project").distinct().count(),
        "benefit_form": BenefitForm(user=request.user),
        "kashf_form": BenefitToKashfForm(user=request.user),
        "aliases": b.aliases.all(),
        "family": services.family_links(b, cd["scope"]),
    }
    return render(request, "registry/beneficiary_detail.html", ctx)


@role_required(*EDIT)
def beneficiary_create(request):
    form = BeneficiaryForm(request.POST or None, user=request.user)
    bform = OptionalBenefitForm(request.POST or None, user=request.user, prefix="benefit")
    if request.method == "POST" and form.is_valid() and bform.is_valid():
        b = form.save(commit=False)
        b.home_project = form.cleaned_data["project"]
        b.save()
        services.log(request.user, "create_beneficiary", b)
        if bform.has_benefit():
            cd = bform.cleaned_data
            services.add_manual_benefit(b, cd["project"] or b.home_project, cd["aid_type"], cd["receipt_date"],
                                        request.user)
        messages.success(request, "تم حفظ المستفيد.")
        return redirect("beneficiary_detail", pk=b.pk)
    return render(request, "registry/beneficiary_form.html", {"form": form, "bform": bform, "title": "إدراج مستفيد"})


@role_required(*EDIT)
def beneficiary_edit(request, pk):
    b = _ben(request, pk)
    old = f"{b.full_name} / {b.national_id} / {b.wife_name} / {b.wife_national_id} / {b.family_size} / {b.social_status}"
    form = BeneficiaryForm(request.POST or None, instance=b, user=request.user)
    if request.method == "POST" and form.is_valid():
        b = form.save()
        services.log(request.user, "edit_beneficiary", b, f"قبل: {old}")
        messages.success(request, "تم تحديث بيانات المستفيد.")
        return redirect("beneficiary_detail", pk=b.pk)
    return render(request, "registry/beneficiary_form.html", {"form": form, "title": "تعديل مستفيد", "b": b})


@require_POST
@role_required(ADMIN)
def beneficiary_delete(request, pk):
    b = _ben(request, pk)
    services.deactivate_beneficiary(b, request.user)
    messages.success(request, "تم إخفاء المستفيد (مع الاحتفاظ بالأثر في السجل).")
    return redirect("search")


@require_POST
@role_required(*EDIT)
def benefit_add(request, pk):
    b = _ben(request, pk)
    form = BenefitForm(request.POST, user=request.user)
    if form.is_valid():
        cd = form.cleaned_data
        created = services.add_manual_benefit(
            b, cd["project"], cd["aid_type"], cd["receipt_date"], request.user, note=cd.get("note", ""))
        messages.success(request, "تمت إضافة الاستفادة." if created else "هذه الاستفادة مسجلة مسبقاً.")
    else:
        messages.error(request, "تعذّرت إضافة الاستفادة: " + " ".join(sum(form.errors.values(), [])))
    return redirect("beneficiary_detail", pk=b.pk)


@require_POST
@role_required(*EDIT)
def benefit_add_kashfs(request, pk):
    """إضافة مستفيد موجود إلى كشف أو عدة كشوف قائمة."""
    b = _ben(request, pk)
    form = BenefitToKashfForm(request.POST, user=request.user)
    if form.is_valid():
        added, dup = services.add_benefit_to_distributions(
            b, form.cleaned_data["distributions"], request.user, form.cleaned_data.get("note", ""))
        messages.success(request, f"أُضيف إلى {added} كشف" + (f" (مسجل مسبقاً في {dup})" if dup else "") + ".")
    else:
        messages.error(request, "اختر كشفاً واحداً على الأقل.")
    return redirect("beneficiary_detail", pk=b.pk)


@role_required(*EDIT)
def benefit_edit(request, pk):
    x = get_object_or_404(scoped_benefits(request).select_related("beneficiary", "project", "aid_type"), pk=pk)
    form = BenefitForm(request.POST or None, user=request.user, initial={
        "project": x.project, "aid_type": x.aid_type, "receipt_date": x.receipt_date, "note": x.note})
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        if services.update_benefit(x, cd["project"], cd["aid_type"], cd["receipt_date"], request.user, cd.get("note", "")):
            messages.success(request, "تم تعديل الاستفادة.")
            return redirect("beneficiary_detail", pk=x.beneficiary_id)
        form.add_error(None, "توجد استفادة مطابقة (نفس المشروع والنوع والتاريخ) لهذا الشخص.")
    return render(request, "registry/benefit_form.html", {"form": form, "x": x})


@require_POST
@role_required(ADMIN)
def benefit_delete(request, pk):
    x = get_object_or_404(scoped_benefits(request).select_related("beneficiary"), pk=pk)
    who = x.beneficiary_id
    services.delete_benefit(x, request.user)
    messages.success(request, "تم حذف الاستفادة.")
    return redirect("beneficiary_detail", pk=who)


@require_POST
@role_required(*EDIT)
def alias_promote(request, pk):
    alias = get_object_or_404(
        NameAlias.objects.filter(beneficiary__in=services.visible_beneficiaries(_scope(request))).select_related("beneficiary"),
        pk=pk)
    who = alias.beneficiary_id
    services.promote_alias(alias, request.user)
    messages.success(request, "اعتُمد الاسم أساسياً وحُفظ الاسم السابق كاسم بديل.")
    return redirect("beneficiary_detail", pk=who)


# ================================================================ المشاريع
@role_required(*EDIT)
def project_detail(request, pk):
    p = get_object_or_404(visible_projects(request.user), pk=pk)
    dists = Distribution.objects.filter(project=p).select_related("aid_type", "upload")
    return render(request, "registry/project_detail.html", {
        "p": p, "o": services.project_overview(p), "dists": dists,
        "uploads": p.uploads.filter(status=Upload.CONFIRMED),
    })


@role_required(*EDIT)
def project_export(request, pk):
    p = get_object_or_404(visible_projects(request.user), pk=pk)
    rows = (
        (i, x.beneficiary.full_name, x.beneficiary.national_id, x.beneficiary.wife_name, x.beneficiary.wife_national_id,
         x.beneficiary.family_size or "", x.beneficiary.social_status, str(x.aid_type),
         x.receipt_date.isoformat(), x.source_label, x.note)
        for i, x in enumerate(services.iter_project_rows(p), start=1)
    )
    services.log(request.user, "export_project", p)
    return xlsx_response(
        f"مشروع-{p.name}.xlsx", f"مشروع {p.name}",
        ["#", "الاسم", "رقم الهوية", "اسم الزوجة", "هوية الزوجة", "عدد الأفراد", "الحالة الاجتماعية", "نوع المساعدة",
         "تاريخ الاستلام", "المصدر (الملف / يدوي)", "ملاحظة"],
        rows, subtitle=f"كل أسماء المشروع: {p.name}",
    )


@role_required(*EDIT)
def projects(request):
    if request.method == "POST":
        if not has_role(request.user, ADMIN):
            raise PermissionDenied              # إنشاء المشاريع لمدير النظام فقط
        name = clean_text(request.POST.get("name"))
        if name:
            p, created = Project.objects.get_or_create(name=name, defaults={"created_by": request.user})
            messages.success(request, "تم إنشاء المشروع." if created else "المشروع موجود مسبقاً.")
            if created:
                services.log(request.user, "create_project", p)
        return redirect("projects")
    rows = visible_projects(request.user).annotate(
        uploads_n=Count("uploads", distinct=True), kashfs_n=Count("distributions", distinct=True),
        members_n=Count("members", distinct=True)).order_by("name")
    return render(request, "registry/projects.html", {"projects": rows})


@role_required(ADMIN)
def project_members(request, pk):
    """مدير النظام يحدد من يعمل على المشروع (مديرو الدوائر والموظفون)."""
    p = get_object_or_404(Project, pk=pk)
    form = ProjectMembersForm(request.POST or None, initial={"members": p.members.all()})
    if request.method == "POST" and form.is_valid():
        p.members.set(form.cleaned_data["members"])
        services.log(request.user, "set_project_members", p, ", ".join(u.username for u in form.cleaned_data["members"]))
        messages.success(request, "تم تحديث المستخدمين المسندين إلى المشروع.")
        return redirect("projects")
    return render(request, "registry/project_members.html", {"p": p, "form": form})


# ================================================================ رفع الملفات
def _default_aid_name(upload):
    return upload.default_aid_type.name if upload.default_aid_type else ""


def _clean_upload(upload):
    table, notes = load_table(upload.file.path)
    cleaned = clean_rows(
        table, upload.mapping, _default_aid_name(upload), upload.default_date,
        id_lengths=settings.NATIONAL_ID_LENGTHS, kind=upload.kind, strict_quad=settings.STRICT_WIFE_QUAD_NAME,
    )
    return table, notes, cleaned


@role_required(*EDIT)
def upload_list(request):
    qs = _uploads(request).select_related("project", "uploaded_by", "default_aid_type")
    return render(request, "registry/upload_list.html", {"page": _page(request, qs, 25), "qs": _qs_without_page(request)})


def _registry_exists():
    return Upload.objects.filter(kind=Upload.REGISTRY, status=Upload.CONFIRMED).exists()


def _ready(upload):
    m = upload.mapping or {}
    need = REGISTRY_REQUIRED if upload.kind == Upload.REGISTRY else REQUIRED_FIELDS
    return upload.status == Upload.DRAFT and all(m.get(f) for f in need) and upload.rows_accepted > 0


@role_required(*EDIT)
def upload_new(request):
    form = UploadForm(request.POST or None, request.FILES or None, initial={"kind": "distribution"}, user=request.user)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        kind, files = cd["kind"], cd["file"]
        project = cd["project"] or Project.objects.get_or_create(
            name=clean_text(cd["new_project"]), defaults={"created_by": request.user})[0]
        aid = None
        if kind == Upload.DISTRIBUTION:
            aid = cd["default_aid_type"]
            if not aid and clean_text(cd["new_aid_type"]):
                aid = services.get_or_create_aid_type(clean_text(cd["new_aid_type"]))
        batch = uuid.uuid4().hex if len(files) > 1 else ""
        created = []
        for f in files:
            upload = Upload.objects.create(
                project=project, file=f, file_name=os.path.basename(f.name), kind=kind, batch=batch,
                default_aid_type=aid, default_date=cd["default_date"] if kind == Upload.DISTRIBUTION else None,
                uploaded_by=request.user,
            )
            try:
                table, _ = load_table(upload.file.path)
                upload.mapping = auto_map(table_columns(table))
                cleaned = clean_rows(
                    table, upload.mapping, _default_aid_name(upload), upload.default_date,
                    id_lengths=settings.NATIONAL_ID_LENGTHS, kind=kind, strict_quad=settings.STRICT_WIFE_QUAD_NAME,
                )
                services.store_raw_rows(upload, table)      # كل أعمدة الملف تُحفظ كما وصلت
            except Exception as exc:  # ملف تالف أو غير مدعوم
                upload.file.delete(save=False)
                upload.delete()
                messages.error(request, f"«{f.name}»: تعذّرت قراءة الملف ({exc})")
                continue
            upload.rows_total = cleaned["total"]
            upload.rows_accepted = len(cleaned["valid"])  # مبدئي حتى الاعتماد
            upload.rows_rejected = len(cleaned["rejected"])
            upload.rows_duplicate = len(cleaned["duplicates"])
            upload.save()
            services.log(request.user, "upload_file", upload)
            created.append(upload)
        if len(created) == 1:
            return redirect("upload_mapping", pk=created[0].pk)
        if created:
            return redirect("upload_batch", batch=batch)
        form.add_error(None, "لم يُقبل أي ملف.")
    return render(request, "registry/upload_form.html", {"form": form})


@role_required(*EDIT)
def upload_batch(request, batch):
    uploads = list(_uploads(request).filter(batch=batch).select_related("project", "default_aid_type").order_by("id"))
    if not uploads:
        raise Http404
    rows = [{"u": u, "ready": _ready(u)} for u in uploads]
    return render(request, "registry/upload_batch.html", {
        "rows": rows, "batch": batch, "ready_count": sum(r["ready"] for r in rows),
        "kind": uploads[0].kind, "registry_exists": _registry_exists(),
    })


@require_POST
@role_required(*EDIT)
def upload_batch_confirm(request, batch):
    uploads = list(_uploads(request).filter(batch=batch, status=Upload.DRAFT).order_by("id"))
    update_names = bool(request.POST.get("update_names"))
    skip = bool(request.POST.get("skip_unregistered"))
    done = failed = 0
    totals = {"benefits": 0, "new_beneficiaries": 0}
    for u in uploads:
        try:
            _, _, cleaned = _clean_upload(u)
        except Exception as exc:
            messages.error(request, f"«{u.file_name}»: {exc}")
            failed += 1
            continue
        if not cleaned["valid"]:
            messages.warning(request, f"«{u.file_name}»: لا توجد صفوف صالحة، لم يُعتمد. راجع المطابقة.")
            failed += 1
            continue
        summary = services.commit_upload(u, cleaned, request.user, update_names, skip_unregistered=skip)
        totals["benefits"] += summary["benefits"]
        totals["new_beneficiaries"] += summary["new_beneficiaries"]
        done += 1
    messages.success(
        request,
        f"اعتُمد {done} ملف: {totals['new_beneficiaries']} مستفيد جديد، {totals['benefits']} استفادة."
        + (f" لم يُعتمد {failed}." if failed else ""),
    )
    return redirect("upload_batch", batch=batch) if failed else redirect("upload_list")


@role_required(*EDIT)
def upload_mapping(request, pk):
    upload = get_object_or_404(_uploads(request), pk=pk, status=Upload.DRAFT)
    table, notes = load_table(upload.file.path)
    columns = table_columns(table)
    registry = upload.kind == Upload.REGISTRY
    FormClass = make_mapping_form(columns, upload.kind)
    initial = {**upload.mapping, "default_aid_type": upload.default_aid_type, "default_date": upload.default_date}
    form = FormClass(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        upload.mapping = {f: cd.get(f, "") for f in FIELDS}
        if not registry:
            aid = cd["default_aid_type"]
            if not aid and clean_text(cd["new_aid_type"]):
                aid = services.get_or_create_aid_type(clean_text(cd["new_aid_type"]))
            upload.default_aid_type = aid
            upload.default_date = cd["default_date"]
        upload.save()
        return redirect("upload_summary", pk=upload.pk)
    preview = table.head(8)[columns].fillna("").astype(str).values.tolist()
    return render(request, "registry/upload_mapping.html", {
        "upload": upload, "form": form, "columns": columns, "preview": preview, "notes": notes,
        "total_rows": len(table), "registry": registry,
    })


@role_required(*EDIT)
def upload_summary(request, pk):
    upload = get_object_or_404(_uploads(request), pk=pk, status=Upload.DRAFT)
    try:
        _, notes, cleaned = _clean_upload(upload)
    except Exception as exc:
        messages.error(request, f"تعذّرت معالجة الملف: {exc}")
        return redirect("upload_mapping", pk=upload.pk)
    stats = services.preview_stats(upload, cleaned)
    upload.rows_total = cleaned["total"]
    upload.rows_accepted = len(cleaned["valid"])
    upload.rows_rejected = len(cleaned["rejected"])
    upload.rows_duplicate = len(cleaned["duplicates"])
    upload.save(update_fields=["rows_total", "rows_accepted", "rows_rejected", "rows_duplicate"])
    return render(request, "registry/upload_summary.html", {
        "upload": upload, "cleaned": cleaned, "stats": stats, "notes": notes,
        "registry": upload.kind == Upload.REGISTRY, "registry_exists": _registry_exists(),
        "rejected": cleaned["rejected"][:100], "duplicates": cleaned["duplicates"][:50],
        "diffs": stats["name_differences"][:50], "unmatched": stats["unmatched_rows"][:50],
        "valid_count": len(cleaned["valid"]),
    })


@require_POST
@role_required(*EDIT)
def upload_confirm(request, pk):
    upload = get_object_or_404(_uploads(request), pk=pk, status=Upload.DRAFT)
    _, _, cleaned = _clean_upload(upload)
    if not cleaned["valid"]:
        messages.error(request, "لا توجد صفوف صالحة للدمج.")
        return redirect("upload_summary", pk=upload.pk)
    summary = services.commit_upload(
        upload, cleaned, request.user,
        update_names=bool(request.POST.get("update_names")),
        skip_unregistered=bool(request.POST.get("skip_unregistered")),
    )
    if summary["kind"] == Upload.REGISTRY:
        messages.success(
            request,
            f"تمت إضافة {summary['new_beneficiaries']} أسرة جديدة (الموجودة مسبقاً: {summary['existing_beneficiaries']}).",
        )
    else:
        extra = f" وتم تخطي {summary['skipped_unregistered']} اسماً غير مسجل." if summary["skipped_unregistered"] else ""
        messages.success(
            request,
            f"تم الدمج: {summary['benefits']} استفادة، {summary['new_beneficiaries']} مستفيد جديد، "
            f"{summary['distributions']} كشف.{extra}",
        )
    return redirect("upload_list")


@require_POST
@role_required(*EDIT)
def upload_cancel(request, pk):
    upload = get_object_or_404(_uploads(request), pk=pk)
    if upload.status == Upload.CONFIRMED and not has_role(request.user, ADMIN):
        raise PermissionDenied
    if upload.status == Upload.CANCELLED:
        return redirect("upload_list")
    services.cancel_upload(upload, request.user)
    messages.success(request, "تم إلغاء عملية الرفع.")
    return redirect("upload_list")


@role_required(*EDIT)
def upload_rejected(request, pk):
    upload = get_object_or_404(_uploads(request), pk=pk)
    _, _, cleaned = _clean_upload(upload)
    rows = [
        (r["sheet"], r["row"], r["name"], r["national_id"], r["reason"]) for r in cleaned["rejected"]
    ] + [(r["sheet"], r["row"], r["name"], r["national_id"], r["reason"]) for r in cleaned["duplicates"]]
    if upload.status == Upload.DRAFT and upload.kind == Upload.DISTRIBUTION:
        stats = services.preview_stats(upload, cleaned)
        rows += [(r["sheet"], r["row"], r["name"], r["national_id"], "غير مسجل في القاعدة") for r in stats["unmatched_rows"]]
    return xlsx_response(f"مرفوض-{upload.file_name}.xlsx", "الصفوف المرفوضة والمكررة",
                         ["الورقة", "رقم الصف", "الاسم", "رقم الهوية", "السبب"], rows)


@role_required(*EDIT)
def upload_merge_report(request, pk):
    """تقرير دمج الهويات (المكررة داخل الملف وعبر الملفات السابقة) كملف إكسل."""
    upload = get_object_or_404(_uploads(request), pk=pk)
    rep_ = upload.report or {}
    if not rep_:
        raise Http404("لا يوجد تقرير: الرفعة غير معتمدة بعد.")
    rows = [
        (i, r["national_id"], r["name"], r["in_file"], "، ".join(str(x) for x in r["file_rows"]),
         "نعم" if r["known_before"] else "لا", r["before"], r["added"], r["after"],
         "؛ ".join(r["dates"]), "؛ ".join(r["prior_files"]))
        for i, r in enumerate(rep_["rows"], start=1)
    ]
    t = rep_["totals"]
    services.log(request.user, "export_merge_report", upload, f"{len(rows)} هوية")
    return xlsx_response(
        f"تقرير-دمج-{upload.file_name}.xlsx", "تقرير دمج الهويات",
        ["#", "رقم الهوية", "الاسم", "مرات ظهورها في الملف", "أرقام الصفوف", "موجودة قبل الرفع", "استفادات قبل",
         "أضيفت الآن", "الإجمالي بعد الدمج", "تواريخ الاستفادة", "الملفات السابقة"],
        rows,
        subtitle=(f"الملف: {upload.file_name} | هويات متكررة داخل الملف: {t['repeated_in_file']} | "
                  f"هويات سبقت في ملفات/سجلات سابقة: {t['repeated_across']} | "
                  f"إجمالي الاستفادات قبل: {t['benefits_before']} ← بعد: {t['benefits_after']}"),
    )


@role_required(*EDIT)
def upload_file(request, pk):
    upload = get_object_or_404(_uploads(request), pk=pk)
    try:
        return FileResponse(upload.file.open("rb"), as_attachment=True, filename=upload.file_name)
    except FileNotFoundError:
        raise Http404("الملف الأصلي غير موجود")


# ================================================================ الكشوفات
@role_required(*EDIT)
def distribution_list(request):
    form, cd = _filters(request)
    qs = services.distributions_queryset(cd)
    return render(request, "registry/distribution_list.html", {
        "form": form, "page": _page(request, qs, 25), "qs": _qs_without_page(request),
        "summary": services.distribution_summary(qs),
    })


def _distribution_rows(dist):
    return dist.benefits.filter(beneficiary__is_active=True).select_related(
        "beneficiary", "distribution__upload").order_by("beneficiary__name_search", "id")


@role_required(*EDIT)
def distribution_detail(request, pk):
    dist = get_object_or_404(_dists(request).select_related("project", "aid_type", "upload"), pk=pk)
    return render(request, "registry/distribution_detail.html", {"d": dist, "rows": _distribution_rows(dist)})


@require_POST
@role_required(*EDIT)
def distribution_add_person(request, pk):
    """إضافة شخص مسجل مسبقاً (برقم هويته) إلى هذا الكشف يدوياً."""
    dist = get_object_or_404(_dists(request), pk=pk)
    b, created = services.add_person_to_distribution(
        dist, request.POST.get("national_id", ""), request.user, request.POST.get("note", ""), scope=_scope(request))
    if not b:
        messages.error(request, "رقم الهوية غير مسجل في القاعدة. أدرجه أولاً من «إدراج مستفيد».")
    elif created:
        messages.success(request, f"أُضيف «{b.full_name}» إلى الكشف.")
    else:
        messages.info(request, f"«{b.full_name}» مسجل مسبقاً في هذا الكشف.")
    return redirect("distribution_detail", pk=dist.pk)


@role_required(*EDIT)
def distribution_export(request, pk):
    dist = get_object_or_404(_dists(request).select_related("project", "aid_type"), pk=pk)
    rows = [
        (i, b.beneficiary.full_name, b.beneficiary.national_id, b.beneficiary.social_status, b.source_label, b.note)
        for i, b in enumerate(_distribution_rows(dist), start=1)
    ]
    services.log(request.user, "export_distribution", dist, f"{len(rows)} صف")
    return xlsx_response(
        f"كشف-{dist.aid_type}-{dist.distribution_date}.xlsx", f"كشف {dist.aid_type}",
        ["#", "الاسم", "رقم الهوية", "الحالة الاجتماعية", "المصدر (الملف / يدوي)", "ملاحظة"], rows,
        subtitle=f"المشروع: {dist.project} | التاريخ: {dist.distribution_date} | عدد الأسماء: {len(rows)}",
    )


@role_required(*ANY)
def report(request):
    """كشف مجمّع حسب نوع المساعدة والتاريخ/الفترة."""
    form, cd = _filters(request)
    ctx = {"form": form, "searched": bool(request.GET) and form.is_valid()}
    if ctx["searched"]:
        qs = services.report_benefits(cd)
        total = qs.count()
        ctx.update(
            total=total, unique=qs.values("beneficiary").distinct().count(),
            qs=_qs_without_page(request),
        )
        if request.GET.get("all") == "1":
            ctx["rows"] = qs
        else:
            ctx["page"] = _page(request, qs, 200)
    return render(request, "registry/report.html", ctx)


@role_required(*ANY)
def report_export(request):
    form, cd = _filters(request)
    if not (request.GET and form.is_valid()):
        raise Http404
    qs = services.report_benefits(cd)
    rows = [
        (i, b.beneficiary.full_name, b.beneficiary.national_id, str(b.aid_type), str(b.project),
         b.receipt_date.isoformat())
        for i, b in enumerate(qs, start=1)
    ]
    services.log(request.user, "export_report", urlencode(request.GET), f"{len(rows)} صف")
    return xlsx_response("كشف-مجمع.xlsx", "كشف مجمّع", ["#", "الاسم", "رقم الهوية", "نوع المساعدة", "المشروع", "تاريخ الاستلام"], rows,
                         subtitle=f"عدد الصفوف: {len(rows)}")


# ================================================================ التدقيق
@role_required(ADMIN)
def audit_list(request):
    qs = AuditLog.objects.all()
    action = request.GET.get("action")
    if action:
        qs = qs.filter(action=action)
    return render(request, "registry/audit_list.html", {
        "page": _page(request, qs, 50), "qs": _qs_without_page(request),
        "actions": AuditLog.objects.values_list("action", flat=True).distinct().order_by("action"),
        "action": action,
    })


# ================================================================ مقارنة ملف مع القاعدة
COMPARE_DIR = "compare"


def _compare_key(token, suffix):
    return f"{COMPARE_DIR}/{token}{suffix}"


def _purge_compare_files(max_age=24 * 3600):
    cutoff = timezone.now() - timedelta(seconds=max_age)
    try:
        _, files = default_storage.listdir(COMPARE_DIR)
        for f in files:
            key = f"{COMPARE_DIR}/{f}"
            if default_storage.get_modified_time(key) < cutoff:
                default_storage.delete(key)
    except Exception:  # التنظيف اختياري ولا يجب أن يوقف المقارنة
        pass


def _compare_file_name(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise Http404
    try:
        with default_storage.open(_compare_key(token, "-name.txt"), "rb") as fh:
            return fh.read().decode("utf-8")
    except FileNotFoundError:
        raise Http404


def _compare_path(token):
    ext = os.path.splitext(_compare_file_name(token))[1].lower()
    try:
        return Path(default_storage.path(_compare_key(token, ext)))
    except FileNotFoundError:
        raise Http404


@role_required(*ANY)
def compare_new(request):
    """رفع ملف للمقارنة مع القاعدة (لا يُحفظ شيء في القاعدة)."""
    if request.method == "POST":
        form = CompareForm(request.POST, request.FILES, user=request.user)
        f = request.FILES.get("file")
        if form.is_valid() and f:
            _purge_compare_files()
            token = uuid.uuid4().hex
            ext = os.path.splitext(f.name)[1].lower()
            default_storage.save(_compare_key(token, ext), f)
            default_storage.save(_compare_key(token, "-name.txt"), ContentFile(f.name.encode("utf-8")))
            params = {k: v for k, v in request.POST.items()
                      if k in ("month_from", "month_to", "date_from", "date_to", "aid_type", "project") and v}
            return redirect(reverse("compare_result", args=[token]) + ("?" + urlencode(params) if params else ""))
        if not f:
            form.add_error("file", "اختر ملفاً للمقارنة.")
    else:
        form = CompareForm(user=request.user)
    return render(request, "registry/compare_new.html", {"form": form})


def _compare_run(request, token):
    path = _compare_path(token)
    form = CompareForm(request.GET or None, user=request.user)
    cd = form.cleaned_data if request.GET and form.is_valid() else {}
    cd = {k: v for k, v in cd.items() if k != "file"}
    cd["scope"] = _scope(request)
    try:
        table, notes = load_table(path)
        mapping = auto_map(table_columns(table))
    except Exception as exc:  # ملف تالف أو غير مقروء
        return form, cd, None, {"error": f"تعذّرت قراءة الملف: {exc}"}
    if not (mapping["national_id"] or mapping["name"]):
        cols = "، ".join(str(c) for c in table_columns(table))
        return form, cd, None, {"error": f"لم يُعثر على عمود رقم الهوية ولا عمود الاسم. الأعمدة الموجودة: {cols}"}
    if mapping["national_id"]:
        m = dict(mapping)
        if not m["name"]:
            m["name"] = m["national_id"]          # ملف هويات فقط: الاسم غير مطلوب
        cleaned = clean_rows(table, m, id_lengths=settings.NATIONAL_ID_LENGTHS, kind="lookup")
        rows, counts = services.compare_file(cleaned, cd, name_given=bool(mapping["name"]))
        counts["mode"] = "id"
    else:
        rows, counts = services.compare_names(table, mapping, cd)      # ملف أسماء فقط
        counts["mode"] = "name"
    counts["notes"] = notes
    return form, cd, rows, counts


@role_required(*ANY)
def compare_result(request, token):
    form, cd, rows, counts = _compare_run(request, token)
    file_name = _compare_file_name(token)
    if rows is None:
        return render(request, "registry/compare_result.html",
                      {"form": form, "error": counts["error"], "token": token, "file_name": file_name})
    show = request.GET.get("show", "")
    shown = [r for r in rows if r["status"] == show] if show else rows
    if request.GET.get("warn"):
        shown = [r for r in rows if r["name_warn"]]
    page = _page(request, shown, per_page=100)
    for r in page.object_list:
        r["details"] = services.benefit_details_text(r["matched"], limit=6)
        r["label"] = dict(services.COMPARE_STATUSES)[r["status"]]
    params = request.GET.copy()
    params.pop("page", None)
    params.pop("show", None)
    params.pop("warn", None)
    base_qs = params.urlencode()
    status_cards = [(k, label, counts[k]) for k, label in services.COMPARE_STATUSES]
    if request.GET and not request.GET.get("page"):
        services.log(request.user, "compare_file", f"{file_name} rows={counts['total']}")
    return render(request, "registry/compare_result.html", {
        "form": form, "token": token, "file_name": file_name, "page": page, "counts": counts,
        "status_cards": status_cards, "show": show, "warn": request.GET.get("warn"),
        "base_qs": base_qs, "qs": _qs_without_page(request),
        "show_label": dict(services.COMPARE_STATUSES).get(show, ""),
    })


@role_required(*ANY)
def compare_export(request, token):
    form, cd, rows, counts = _compare_run(request, token)
    if rows is None:
        raise Http404
    labels = dict(services.COMPARE_STATUSES)
    show = request.GET.get("show", "")
    if show:
        rows = [r for r in rows if r["status"] == show]
    out = (
        (i, r["row"], r["name"], r["national_id"], r["db_name"],
         r["similarity"] if r["similarity"] is not None else "",
         labels[r["status"]] + ((" — " + r["reason"]) if r["reason"] else ""),
         r["matched_count"], r["total"], r["types_text"], services.benefit_details_text(r["matched"]),
         r["last_date"].isoformat() if r["last_date"] else "")
        for i, r in enumerate(rows, start=1)
    )
    start, end = services.date_range(cd)
    sub = "الفترة: " + (f"{start or '…'} إلى {end or '…'}" if (start or end) else "كل الفترات")
    if cd.get("aid_type"):
        sub += f" | النوع: {cd['aid_type']}"
    if cd.get("project"):
        sub += f" | المشروع: {cd['project']}"
    services.log(request.user, "export_compare", urlencode(request.GET))
    return xlsx_response(
        "نتيجة-المقارنة.xlsx", "نتيجة مقارنة ملف",
        ["#", "الصف في الملف", "الاسم في الملف", "رقم الهوية", "الاسم في القاعدة", "تشابه الاسم %",
         "نتيجة المقارنة", "عدد مرات الاستفادة (ضمن الفلتر)", "إجمالي مرات الاستفادة", "الأنواع", "تفاصيل الاستفادة (نوع — تاريخ — مشروع)",
         "آخر استلام"],
        out, subtitle=sub,
    )


# ================================================================ الإحصاءات والتصدير الكامل
@role_required(*EDIT)
def stats(request):
    return render(request, "registry/stats.html", {"s": services.overview_stats(_scope(request))})


@role_required(ADMIN)
def export_all(request):
    """القاعدة الموحدة كاملة: كل الاستفادات مع بيانات الشخص والمصدر."""
    qs = (
        Benefit.objects.filter(beneficiary__is_active=True)
        .select_related("beneficiary", "aid_type", "project", "distribution__upload")
        .order_by("receipt_date", "id")
    )
    rows = (
        (i, x.beneficiary.full_name, x.beneficiary.national_id, x.beneficiary.wife_name, x.beneficiary.wife_national_id,
         x.beneficiary.family_size or "", x.beneficiary.social_status, str(x.aid_type),
         str(x.project), x.receipt_date.isoformat(), x.source_label, x.note)
        for i, x in enumerate(qs.iterator(chunk_size=2000), start=1)
    )
    services.log(request.user, "export_all", "القاعدة الموحدة")
    return xlsx_response(
        "القاعدة-الموحدة.xlsx", "القاعدة الموحدة",
        ["#", "الاسم", "رقم الهوية", "اسم الزوجة", "هوية الزوجة", "عدد الأفراد", "الحالة الاجتماعية", "نوع المساعدة", "المشروع",
         "تاريخ الاستلام", "المصدر", "ملاحظة"],
        rows, subtitle="كل الاستفادات المسجلة",
    )


# ================================================================ المستخدمون والصلاحيات (مدير النظام)
def _user_role(u):
    names = set(u.groups.values_list("name", flat=True))
    return next((r for r in ALL_ROLES if r in names), "")


@role_required(ADMIN)
def user_list(request):
    from django.contrib.auth import get_user_model
    users = list(get_user_model().objects.order_by("username").prefetch_related("groups", "assigned_projects"))
    for u in users:
        u.role_name = "مدير النظام (خارق)" if u.is_superuser else (_user_role(u) or "—")
        u.project_names = "كل المشاريع" if (u.is_superuser or u.role_name == ADMIN) else "، ".join(p.name for p in u.assigned_projects.all())
    return render(request, "registry/users.html", {"users": users})


def _save_user(form, instance=None):
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group
    cd = form.cleaned_data
    u = instance or get_user_model()()
    u.username = cd["username"]
    u.first_name = cd.get("full_name", "")
    if "is_active" in cd:
        u.is_active = cd["is_active"]
    elif instance is None:
        u.is_active = True
    if cd.get("password"):
        u.set_password(cd["password"])
    role = cd.get("role")
    if role:
        u.is_staff = role == ADMIN
    u.save()
    if role:
        u.groups.remove(*Group.objects.filter(name__in=ALL_ROLES))
        u.groups.add(Group.objects.get_or_create(name=role)[0])
    u.assigned_projects.set(cd.get("projects") or [])
    return u


@role_required(ADMIN)
def user_create(request):
    form = UserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        u = _save_user(form)
        services.log(request.user, "create_user", u.username, form.cleaned_data["role"])
        messages.success(request, "تم إنشاء المستخدم.")
        return redirect("user_list")
    return render(request, "registry/user_form.html", {"form": form, "title": "مستخدم جديد"})


@role_required(ADMIN)
def user_edit(request, pk):
    from django.contrib.auth import get_user_model
    u = get_object_or_404(get_user_model(), pk=pk)
    me = u.pk == request.user.pk
    initial = {"username": u.username, "full_name": u.first_name, "role": _user_role(u), "is_active": u.is_active,
               "projects": u.assigned_projects.all()}
    form = UserForm(request.POST or None, initial=initial, instance=u, lock_self=me)
    if request.method == "POST" and form.is_valid():
        _save_user(form, u)
        services.log(request.user, "edit_user", u.username, form.cleaned_data.get("role", ""))
        messages.success(request, "تم تحديث المستخدم.")
        return redirect("user_list")
    return render(request, "registry/user_form.html", {"form": form, "title": f"تعديل المستخدم {u.username}", "edit": True})
