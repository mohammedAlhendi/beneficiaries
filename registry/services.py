"""منطق الأعمال: الدمج، الإلغاء، الاستعلام، التدقيق."""
import calendar
import datetime as dt
import difflib

from django.db import transaction
from django.db.models import Count, F, Max, Q, Sum
from django.utils import timezone

from .models import AidType, AuditLog, Benefit, Beneficiary, Distribution, NameAlias, UploadRow
from .textutils import clean_id, clean_text, normalize_arabic

CHUNK = 500


def _chunks(seq, n=CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- نطاق المشاريع
# scope: None = كل المشاريع (مدير النظام)، أو قائمة معرّفات المشاريع المسندة للمستخدم.
def scope_q(scope, field="project_id"):
    return Q() if scope is None else Q(**{f"{field}__in": scope})


def visible_beneficiaries(scope):
    """المستفيدون الفعّالون الظاهرون لنطاق ما: سُجّلوا في مشروعه أو رُفعوا فيه أو لهم استفادة فيه."""
    qs = Beneficiary.objects.filter(is_active=True)
    if scope is None:
        return qs
    return qs.filter(
        Q(home_project_id__in=scope)
        | Q(created_by_upload__project_id__in=scope)
        | Q(pk__in=Benefit.objects.filter(project_id__in=scope).values("beneficiary_id"))
    )


def scoped_benefits(scope):
    return Benefit.objects.filter(scope_q(scope))


# ---------------------------------------------------------------- التدقيق
def log(user, action, obj="", details=""):
    AuditLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        username=getattr(user, "username", "") or "",
        action=action,
        object_repr=str(obj)[:300],
        details=str(details)[:5000],
    )


# ---------------------------------------------------------------- أنواع المساعدات
def aid_type_map():
    return {normalize_arabic(a.name): a for a in AidType.objects.all()}


def resolve_aid_type(name, cache):
    key = normalize_arabic(name)
    if key not in cache:
        cache[key] = AidType.objects.create(name=name.strip())
    return cache[key]


def get_or_create_aid_type(name):
    return resolve_aid_type(name, aid_type_map())


# ---------------------------------------------------------------- حفظ الصفوف الخام
def store_raw_rows(upload, table):
    """يحفظ كل صفوف الملف بكل أعمدتها كما رُفعت (نص)، قبل أي تنقية، دون فقدان أي عمود."""
    cols = [c for c in table.columns if not str(c).startswith("__")]
    batch = []
    for rec in table.to_dict("records"):
        data = {c: clean_text(rec.get(c)) for c in cols}
        batch.append(UploadRow(upload=upload, sheet=str(rec.get("__sheet__", ""))[:100],
                               row_number=rec.get("__row__"), data=data))
    UploadRow.objects.bulk_create(batch, batch_size=CHUNK)
    return len(batch)


# ---------------------------------------------------------------- كشوفات
def refresh_distribution(dist):
    count = dist.benefits.filter(beneficiary__is_active=True).count()
    if count == 0 and dist.upload_id is None:
        dist.delete()
        return None
    dist.names_count = count
    dist.save(update_fields=["names_count"])
    return dist


def _existing_benefit_keys(project, beneficiary_ids):
    keys = set()
    for ch in _chunks(beneficiary_ids):
        keys.update(
            Benefit.objects.filter(project=project, beneficiary_id__in=ch)
            .values_list("beneficiary_id", "aid_type_id", "receipt_date")
        )
    return keys


# ---------------------------------------------------------------- معاينة قبل الاعتماد
def preview_stats(upload, cleaned):
    valid = cleaned["valid"]
    registry = upload.kind == upload.REGISTRY
    ids = sorted({r["national_id"] for r in valid})
    existing = {}
    for ch in _chunks(ids):
        for pk, nid, name in Beneficiary.objects.filter(national_id__in=ch).values_list("pk", "national_id", "full_name"):
            existing[nid] = (pk, name)

    name_diffs, seen = [], set()
    for r in valid:
        nid = r["national_id"]
        if nid in existing and nid not in seen:
            seen.add(nid)
            if normalize_arabic(existing[nid][1]) != normalize_arabic(r["name"]):
                name_diffs.append({"national_id": nid, "old": existing[nid][1], "new": r["name"]})

    id_rows = cleaned.get("id_rows", {})
    repeated_in_file = sum(1 for rows in id_rows.values() if len(rows) > 1)
    stats = {
        "repeated_in_file": repeated_in_file,
        "repeated_across": 0,
        "unique_ids": len(ids),
        "existing_beneficiaries": len(existing),
        "new_beneficiaries": len(ids) - len(existing),
        "name_differences": name_diffs,
        "new_aid_types": [],
        "duplicates_in_db": 0,
        "distributions": 0,
        "unmatched_rows": [],
    }
    if registry:
        stats["repeated_across"] = len(existing)
        return stats

    with_prior = set()
    for ch in _chunks([pk for pk, _ in existing.values()]):
        with_prior.update(Benefit.objects.filter(beneficiary_id__in=ch).values_list("beneficiary_id", flat=True))
    stats["repeated_across"] = len(with_prior)

    cache = aid_type_map()
    stats["new_aid_types"] = sorted({r["aid_type"] for r in valid if normalize_arabic(r["aid_type"]) not in cache})
    keys = _existing_benefit_keys(upload.project, [pk for pk, _ in existing.values()])
    for r in valid:
        info = existing.get(r["national_id"])
        at = cache.get(normalize_arabic(r["aid_type"]))
        if info and at and (info[0], at.pk, r["date"]) in keys:
            stats["duplicates_in_db"] += 1
    stats["distributions"] = len({(normalize_arabic(r["aid_type"]), r["date"]) for r in valid})
    stats["unmatched_rows"] = [r for r in valid if r["national_id"] not in existing]
    return stats


# ---------------------------------------------------------------- الدمج
@transaction.atomic
def commit_upload(upload, cleaned, user=None, update_names=False, skip_unregistered=False):
    registry = upload.kind == upload.REGISTRY
    valid = cleaned["valid"]
    aid_cache = aid_type_map()
    first = {}
    for r in valid:
        first.setdefault(r["national_id"], r)

    existing = {}
    for ch in _chunks(first):
        for b in Beneficiary.objects.filter(national_id__in=ch):
            existing[b.national_id] = b

    skipped_unregistered = 0
    if skip_unregistered and not registry:
        kept = [r for r in valid if r["national_id"] in existing]
        skipped_unregistered = len(valid) - len(kept)
        valid = kept
        first = {nid: r for nid, r in first.items() if nid in existing}

    new_rows = [r for nid, r in first.items() if nid not in existing]
    Beneficiary.objects.bulk_create(
        [
            Beneficiary(
                national_id=r["national_id"], full_name=r["name"],
                name_search=normalize_arabic(r["name"]), social_status=r["social_status"],
                wife_name=r.get("wife_name", ""), wife_name_search=normalize_arabic(r.get("wife_name", "")),
                wife_national_id=r.get("wife_national_id", ""), family_size=r.get("family_size"),
                manager_notes=r.get("notes", ""), home_project=upload.project,
                created_by_upload=upload,
            )
            for r in new_rows
        ],
        batch_size=CHUNK,
    )
    new_ids = {r["national_id"] for r in new_rows}
    for ch in _chunks(new_ids):
        for b in Beneficiary.objects.filter(national_id__in=ch):
            existing[b.national_id] = b

    now = timezone.now()
    to_update, name_diff_count, reactivated, aliases = [], 0, [], []
    for nid, r in first.items():
        if nid in new_ids:
            continue
        b = existing[nid]
        changed = False
        if normalize_arabic(b.full_name) != normalize_arabic(r["name"]):
            name_diff_count += 1
            if update_names:
                aliases.append(NameAlias(beneficiary=b, name=b.full_name, source="الاسم المعتمد سابقاً", upload=upload))
                b.full_name, b.name_search = r["name"], normalize_arabic(r["name"])
                changed = True
            else:
                aliases.append(NameAlias(beneficiary=b, name=r["name"], source=upload.file_name, upload=upload))
        if not b.social_status and r["social_status"]:
            b.social_status = r["social_status"]
            changed = True
        for attr, key in (("wife_name", "wife_name"), ("wife_national_id", "wife_national_id"),
                          ("family_size", "family_size"), ("manager_notes", "notes")):
            if not getattr(b, attr) and r.get(key):      # تُكمَّل الحقول الناقصة فقط ولا يُكتب فوق المسجَّل
                setattr(b, attr, r[key])
                changed = True
        if not b.wife_name_search and b.wife_name:
            b.wife_name_search = normalize_arabic(b.wife_name)
        if b.home_project_id is None:
            b.home_project = upload.project
            changed = True
        if not b.is_active:
            b.is_active = True
            reactivated.append(b.pk)
            changed = True
        if changed:
            b.updated_at = now
            to_update.append(b)
    Beneficiary.objects.bulk_update(
        to_update, ["full_name", "name_search", "social_status", "is_active", "updated_at", "wife_name",
                    "wife_name_search", "wife_national_id", "family_size", "manager_notes", "home_project"],
        batch_size=CHUNK
    )

    for al in aliases:
        al.name_search = normalize_arabic(al.name)
    NameAlias.objects.bulk_create(aliases, batch_size=CHUNK, ignore_conflicts=True)

    prior = _prior_history(existing)
    created_benefits = skipped = distributions = 0
    new_by_nid = {}
    if not registry:
        groups = {}
        for r in valid:
            at = resolve_aid_type(r["aid_type"], aid_cache)
            groups.setdefault((at.pk, r["date"]), {"aid": at, "rows": []})["rows"].append(r)

        keys = _existing_benefit_keys(upload.project, [b.pk for b in existing.values()])
        for (_, date), group in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            dist = Distribution.objects.create(
                upload=upload, project=upload.project, aid_type=group["aid"], distribution_date=date
            )
            batch = []
            for r in group["rows"]:
                b = existing[r["national_id"]]
                key = (b.pk, group["aid"].pk, date)
                if key in keys:
                    skipped += 1
                    continue
                keys.add(key)
                new_by_nid.setdefault(r["national_id"], []).append(date)
                batch.append(Benefit(
                    beneficiary=b, distribution=dist, project=upload.project,
                    aid_type=group["aid"], receipt_date=date, source=Benefit.UPLOAD,
                ))
            Benefit.objects.bulk_create(batch, batch_size=CHUNK)
            if batch:
                dist.names_count = len(batch)
                dist.save(update_fields=["names_count"])
                distributions += 1
                created_benefits += len(batch)
            else:
                dist.delete()

    for dist in Distribution.objects.filter(benefits__beneficiary_id__in=reactivated).distinct():
        refresh_distribution(dist)

    upload.rows_total = cleaned["total"]
    upload.rows_accepted = len(first) if registry else created_benefits
    upload.rows_rejected = len(cleaned["rejected"]) + skipped_unregistered
    upload.rows_duplicate = len(cleaned["duplicates"]) + skipped
    upload.status = upload.CONFIRMED
    upload.confirmed_at = now
    upload.report = build_merge_report(upload, cleaned, first, existing, new_ids, prior, new_by_nid)
    upload.save()

    summary = {
        "kind": upload.kind, "benefits": created_benefits, "new_beneficiaries": len(new_rows),
        "existing_beneficiaries": len(first) - len(new_rows),
        "skipped_existing": skipped, "skipped_unregistered": skipped_unregistered,
        "distributions": distributions, "name_differences": name_diff_count,
    }
    log(user, "confirm_upload", upload, summary)
    return summary


def _prior_history(existing):
    """لكل هوية موجودة قبل هذا الرفع: تواريخ استفاداتها السابقة وأسماء الملفات المصدر."""
    hist = {}
    by_pk = {b.pk: nid for nid, b in existing.items()}
    for ch in _chunks(by_pk):
        for bid, d, fname, src in Benefit.objects.filter(beneficiary_id__in=ch).values_list(
                "beneficiary_id", "receipt_date", "distribution__upload__file_name", "source"):
            h = hist.setdefault(by_pk[bid], {"dates": [], "files": set()})
            h["dates"].append(d)
            h["files"].add(fname or ("إدخال يدوي" if src == Benefit.MANUAL else "ملف محذوف"))
    return hist


def build_merge_report(upload, cleaned, first, existing, new_ids, prior, new_by_nid):
    """تقرير دمج الهويات: الهويات المتكررة داخل الملف أو مع ما رُفع سابقاً، مع عدد الاستفادات قبل/بعد وتواريخها."""
    id_rows = cleaned.get("id_rows", {})
    rows = []
    for nid, r in first.items():
        in_file = len(id_rows.get(nid, [])) or 1
        old = prior.get(nid, {"dates": [], "files": set()})
        was_known = nid not in new_ids
        added_dates = sorted(new_by_nid.get(nid, []))
        if in_file <= 1 and not was_known:
            continue                                      # ظهر مرة واحدة وهو جديد: لا تكرار
        before = len(old["dates"])
        rows.append({
            "national_id": nid, "name": existing[nid].full_name, "in_file": in_file,
            "file_rows": id_rows.get(nid, []), "known_before": was_known,
            "before": before, "added": len(added_dates), "after": before + len(added_dates),
            "dates": [d.isoformat() for d in sorted(old["dates"] + added_dates)],
            "prior_files": sorted(old["files"]),
        })
    rows.sort(key=lambda x: (-x["after"], x["national_id"]))
    return {
        "totals": {
            "ids_in_file": len(first), "repeated_in_file": sum(1 for x in rows if x["in_file"] > 1),
            "repeated_across": sum(1 for x in rows if x["known_before"]),
            "benefits_before": sum(x["before"] for x in rows), "benefits_added": sum(x["added"] for x in rows),
            "benefits_after": sum(x["after"] for x in rows),
        },
        "rows": rows,
    }


@transaction.atomic
def cancel_upload(upload, user=None):
    """يلغي عملية رفع: يحذف كشوفاتها واستفاداتها والمستفيدين الذين أنشأتهم ولا استفادة أخرى لهم."""
    was_confirmed = upload.status == upload.CONFIRMED
    if was_confirmed:
        Distribution.objects.filter(upload=upload).delete()
        created = Beneficiary.objects.filter(created_by_upload=upload)
        created.filter(benefits__isnull=True).delete()
        Beneficiary.objects.filter(created_by_upload=upload).update(created_by_upload=None)
    upload.status = upload.CANCELLED
    upload.save(update_fields=["status"])
    log(user, "cancel_upload" if was_confirmed else "discard_upload", upload)


# ---------------------------------------------------------------- إدخال يدوي
def find_or_create_distribution(project, aid_type, date):
    """الكشف المطابق (مشروع + نوع + تاريخ): يُفضَّل كشف الملف المرفوع، وإلا يُنشأ كشف يدوي.
    بهذا يظهر المُدرَج يدوياً تلقائياً في كشف المشروع نفسه وفي تصديره."""
    dist = (
        Distribution.objects.filter(project=project, aid_type=aid_type, distribution_date=date)
        .order_by(F("upload").asc(nulls_last=True), "id").first()
    )
    return dist or Distribution.objects.create(project=project, aid_type=aid_type, distribution_date=date)


@transaction.atomic
def add_manual_benefit(beneficiary, project, aid_type, date, user=None, distribution=None, note=""):
    """يسجّل استفادة يدوية لمستفيد مسجل. إن مُرِّر كشف يُستخدم هو (ومنه المشروع والنوع والتاريخ)."""
    if distribution is not None:
        project, aid_type, date = distribution.project, distribution.aid_type, distribution.distribution_date
        dist = distribution
    else:
        dist = find_or_create_distribution(project, aid_type, date)
    _, created = Benefit.objects.get_or_create(
        beneficiary=beneficiary, project=project, aid_type=aid_type, receipt_date=date,
        defaults={"distribution": dist, "source": Benefit.MANUAL, "note": clean_text(note)[:300], "added_by": user},
    )
    refresh_distribution(dist)
    if created:
        log(user, "add_benefit", beneficiary, f"{project} / {aid_type} / {date} → كشف #{dist.pk}")
    return created


def add_benefit_to_distributions(beneficiary, distributions, user=None, note=""):
    """يضيف المستفيد إلى عدة كشوف دفعة واحدة؛ يعيد (عدد المضاف، عدد المكرر)."""
    added = dup = 0
    for d in distributions:
        if add_manual_benefit(beneficiary, None, None, None, user, distribution=d, note=note):
            added += 1
        else:
            dup += 1
    return added, dup


def add_person_to_distribution(dist, national_id, user=None, note="", scope=None):
    """يضيف شخصاً مسجلاً (برقم هويته) إلى كشف. يعيد (beneficiary|None, created)."""
    nid = clean_id(national_id)
    b = visible_beneficiaries(scope).filter(national_id=nid).first() if nid else None
    if not b:
        return None, False
    return b, add_manual_benefit(b, None, None, None, user, distribution=dist, note=note)


@transaction.atomic
def update_benefit(benefit, project, aid_type, date, user=None, note=None):
    """تصحيح استفادة (مشروع/نوع/تاريخ). يعيد False إن وُجدت استفادة مطابقة أخرى."""
    clash = Benefit.objects.filter(
        beneficiary=benefit.beneficiary, project=project, aid_type=aid_type, receipt_date=date
    ).exclude(pk=benefit.pk).exists()
    if clash:
        return False
    old = f"{benefit.project} / {benefit.aid_type} / {benefit.receipt_date}"
    old_dist = benefit.distribution
    changed_key = (benefit.project_id, benefit.aid_type_id, benefit.receipt_date) != (project.pk, aid_type.pk, date)
    if changed_key:
        benefit.distribution = find_or_create_distribution(project, aid_type, date)
    benefit.project, benefit.aid_type, benefit.receipt_date = project, aid_type, date
    if note is not None:
        benefit.note = clean_text(note)[:300]
    benefit.save()
    if changed_key:
        refresh_distribution(old_dist)
        refresh_distribution(benefit.distribution)
    log(user, "edit_benefit", benefit.beneficiary, f"{old} → {project} / {aid_type} / {date}")
    return True


@transaction.atomic
def delete_benefit(benefit, user=None):
    dist, who = benefit.distribution, benefit.beneficiary
    desc = f"{benefit.project} / {benefit.aid_type} / {benefit.receipt_date}"
    benefit.delete()
    refresh_distribution(dist)
    log(user, "delete_benefit", who, desc)


@transaction.atomic
def promote_alias(alias, user=None):
    """يعتمد الاسم البديل اسماً أساسياً، ويصير الاسم السابق بديلاً (لا يضيع شيء)."""
    b = alias.beneficiary
    old = b.full_name
    b.full_name = alias.name
    b.save()
    alias.delete()
    NameAlias.objects.get_or_create(beneficiary=b, name_search=normalize_arabic(old), defaults={"name": old, "source": "الاسم المعتمد سابقاً"})
    log(user, "promote_name", b, f"{old} → {b.full_name}")


@transaction.atomic
def deactivate_beneficiary(beneficiary, user=None):
    beneficiary.is_active = False
    beneficiary.save()
    for dist in Distribution.objects.filter(benefits__beneficiary=beneficiary).distinct():
        refresh_distribution(dist)
    log(user, "delete_beneficiary", beneficiary)


# ---------------------------------------------------------------- الفلترة والبحث
def date_range(cd):
    """(بداية، نهاية) من حقول الأشهر أو التواريخ في نموذج الفلترة."""
    start = end = None
    mf, mt = cd.get("month_from"), cd.get("month_to")
    if mf:
        start = mf
    if mt:
        end = dt.date(mt.year, mt.month, calendar.monthrange(mt.year, mt.month)[1])
    if not start and cd.get("date_from"):
        start = cd["date_from"]
    if not end and cd.get("date_to"):
        end = cd["date_to"]
    return start, end


def benefit_q(cd, prefix="", date_field="receipt_date"):
    q = scope_q(cd.get("scope"), f"{prefix}project_id")
    start, end = date_range(cd)
    if start:
        q &= Q(**{f"{prefix}{date_field}__gte": start})
    if end:
        q &= Q(**{f"{prefix}{date_field}__lte": end})
    if cd.get("aid_type"):
        q &= Q(**{f"{prefix}aid_type": cd["aid_type"]})
    if cd.get("project"):
        q &= Q(**{f"{prefix}project": cd["project"]})
    return q


def has_benefit_filters(cd):
    start, end = date_range(cd)
    return bool(start or end or cd.get("aid_type") or cd.get("project"))


def search_beneficiaries(cd):
    """استعلام البحث (بدون تجميعات). status: with (استفاد) / without (لم يستفد ضمن الفلاتر)."""
    qs = visible_beneficiaries(cd.get("scope"))
    text = (cd.get("q") or "").strip()
    if text:
        as_id = clean_id(text)
        if as_id.isdigit():
            # رقم هوية الزوج أو الزوجة: مطابقة تامة إن كان الرقم كاملاً، وإلا بحث جزئي
            qs = qs.filter(Q(national_id__contains=as_id) | Q(wife_national_id__contains=as_id))
        else:
            for token in normalize_arabic(text).split():
                qs = qs.filter(Q(name_search__contains=token) | Q(wife_name_search__contains=token))
    filtered = has_benefit_filters(cd)
    status = cd.get("status") or ""
    matching = Benefit.objects.filter(benefit_q(cd)).values("beneficiary_id")
    if status == "without":
        qs = qs.exclude(pk__in=matching)
    elif status == "with" or filtered:
        qs = qs.filter(pk__in=matching)
    return qs.order_by("name_search", "id"), filtered


def match_kind(b, text):
    """بماذا طابق المستفيد نص البحث: هويته، هوية زوجته، أو الاسم."""
    as_id = clean_id(text or "")
    if as_id.isdigit():
        if as_id in b.national_id:
            return "هوية المستفيد"
        if as_id in (b.wife_national_id or ""):
            return "هوية الزوجة"
    return ""


def suggest(text, scope, limit=8):
    """اقتراحات فورية أثناء الكتابة (هوية الزوج/الزوجة أو الاسم)."""
    text = (text or "").strip()
    if len(text) < 2:
        return []
    cd = {"q": text, "scope": scope}
    qs, _ = search_beneficiaries(cd)
    out = []
    for b in qs[:limit]:
        out.append({"id": b.pk, "name": b.full_name, "national_id": b.national_id,
                    "wife_name": b.wife_name, "wife_national_id": b.wife_national_id,
                    "via": match_kind(b, text)})
    return out


def family_links(b, scope):
    """الروابط الأسرية: زوجة مسجلة كمستفيد، أو مستفيد ترد هويته كزوجة/زوج هذا الشخص، أو من يشاركه هوية الزوجة."""
    q = Q()
    if b.wife_national_id:
        q |= Q(national_id=b.wife_national_id) | Q(wife_national_id=b.wife_national_id)
    q |= Q(wife_national_id=b.national_id)
    links = visible_beneficiaries(scope).filter(q).exclude(pk=b.pk)[:10]
    out = []
    for o in links:
        if o.national_id == b.wife_national_id:
            rel = "الزوجة مسجلة كمستفيدة"
        elif o.wife_national_id == b.national_id:
            rel = "سجل يرد فيه هذا الشخص كزوجة/زوج"
        else:
            rel = "يشترك معه في هوية الزوجة"
        out.append((o, rel))
    return out


def annotate_beneficiaries(items, cd):
    """يضيف أعداد الاستفادة لقائمة قصيرة من المستفيدين (صفحة واحدة) بدل تجميع القاعدة كلها."""
    items = list(items)
    ids = [b.pk for b in items]
    rows = (
        Benefit.objects.filter(beneficiary_id__in=ids).values("beneficiary_id")
        .annotate(
            total=Count("id"), projects=Count("project", distinct=True), last=Max("receipt_date"),
            matched=Count("id", filter=benefit_q(cd)),
        )
    )
    agg = {r["beneficiary_id"]: r for r in rows}
    types = {}
    for r in (
        Benefit.objects.filter(beneficiary_id__in=ids).values("beneficiary_id", "aid_type__name")
        .annotate(n=Count("id")).order_by("aid_type__name")
    ):
        types.setdefault(r["beneficiary_id"], []).append({"name": r["aid_type__name"], "n": r["n"]})
    for b in items:
        r = agg.get(b.pk)
        b.total_benefits = r["total"] if r else 0
        b.projects_count = r["projects"] if r else 0
        b.last_date = r["last"] if r else None
        b.matched_benefits = r["matched"] if r else 0
        b.aid_types = types.get(b.pk, [])
        b.aid_types_text = "، ".join(f'{t["name"]} ×{t["n"]}' if t["n"] > 1 else t["name"] for t in b.aid_types)
    return items


def iter_search_rows(cd, chunk=2000):
    """مولّد لصفوف التصدير (كل النتائج) على دفعات."""
    qs, _ = search_beneficiaries(cd)
    batch = []
    for b in qs.iterator(chunk_size=chunk):
        batch.append(b)
        if len(batch) >= chunk:
            yield from annotate_beneficiaries(batch, cd)
            batch = []
    if batch:
        yield from annotate_beneficiaries(batch, cd)


def distributions_queryset(cd):
    qs = Distribution.objects.select_related("project", "aid_type").filter(
        benefit_q(cd, date_field="distribution_date")
    )
    return qs.filter(names_count__gt=0)


def distribution_summary(qs):
    totals = qs.aggregate(kashfs=Count("id"), names=Sum("names_count"))
    by_type = list(
        qs.values("aid_type__name").annotate(kashfs=Count("id"), names=Sum("names_count")).order_by("-names")
    )
    return {"kashfs": totals["kashfs"] or 0, "names": totals["names"] or 0, "by_type": by_type}


def report_benefits(cd):
    qs = Benefit.objects.select_related("beneficiary", "aid_type", "project").filter(
        beneficiary__is_active=True
    ).filter(benefit_q(cd))
    return qs.order_by("receipt_date", "beneficiary__name_search", "id")


# ---------------------------------------------------------------- مقارنة ملف مع القاعدة
COMPARE_STATUSES = [
    ("prior", "سبق له الاستفادة"),
    ("other", "استفاد خارج الفترة/الفلاتر"),
    ("new", "مسجل ولم يستفد"),
    ("unregistered", "غير مسجل في القاعدة"),
    ("review", "يحتاج مراجعة"),
]
NAME_MATCH_THRESHOLD = 80


def name_similarity(a, b):
    """نسبة تشابه الاسمين (0-100) بعد توحيد الحروف العربية."""
    a, b = normalize_arabic(a or ""), normalize_arabic(b or "")
    if not a or not b:
        return 0
    return round(difflib.SequenceMatcher(None, a, b).ratio() * 100)


def compare_file(cleaned, cd, name_given=True):
    """يقارن صفوف ملف منقّى (clean_rows) مع القاعدة دون حفظ أي شيء.

    cd: فلاتر الفترة (تاريخ/شهر) ونوع المساعدة والمشروع. الاستفادة «ضمن الفلتر» هي ما يطابقها،
    وما عداها يُعدّ سابقاً خارج الفلتر. يعيد (rows, counts).
    """
    valid = cleaned["valid"]
    ids = [r["national_id"] for r in valid]
    bens = {}
    for part in _chunks(ids):
        for b in visible_beneficiaries(cd.get("scope")).filter(national_id__in=part):
            bens[b.national_id] = b
    by_id = {b.pk: [] for b in bens.values()}
    filtered = has_benefit_filters(cd)
    start, end = date_range(cd)
    q = benefit_q(cd)
    pks = list(by_id)
    for part in _chunks(pks):
        for r in (
            Benefit.objects.filter(beneficiary_id__in=part).filter(scope_q(cd.get("scope")))
            .values("beneficiary_id", "receipt_date", "aid_type__name", "project__name")
            .order_by("-receipt_date")
        ):
            by_id[r["beneficiary_id"]].append(r)

    def in_filter(r):
        if start and r["receipt_date"] < start:
            return False
        if end and r["receipt_date"] > end:
            return False
        if cd.get("aid_type") and r["aid_type__name"] != cd["aid_type"].name:
            return False
        if cd.get("project") and r["project__name"] != cd["project"].name:
            return False
        return True

    rows = []
    for item in valid:
        b = bens.get(item["national_id"])
        row = {
            "row": item["row"], "national_id": item["national_id"], "name": item["name"],
            "db_name": "", "similarity": None, "name_warn": False, "reason": "",
            "matched": [], "total": 0, "matched_count": 0, "last_date": None,
        }
        if not b:
            row["status"] = "unregistered"
        else:
            history = by_id[b.pk]
            matched = [h for h in history if in_filter(h)]
            row.update(
                db_name=b.full_name, similarity=name_similarity(item["name"], b.full_name),
                matched=matched, total=len(history), matched_count=len(matched),
                last_date=history[0]["receipt_date"] if history else None,
            )
            if not name_given:
                row["similarity"], row["name"] = None, ""
            row["name_warn"] = name_given and row["similarity"] < NAME_MATCH_THRESHOLD
            row["status"] = "prior" if matched else ("other" if history else "new")
        rows.append(row)
    for r in cleaned["rejected"]:
        rows.append({
            "row": r["row"], "national_id": r["national_id"], "name": r["name"], "db_name": "",
            "similarity": None, "name_warn": False, "status": "review", "reason": r["reason"],
            "matched": [], "total": 0, "matched_count": 0, "last_date": None,
        })
    for r in rows:
        tally = {}
        for h in r["matched"]:
            tally[h["aid_type__name"]] = tally.get(h["aid_type__name"], 0) + 1
        r["types_text"] = "، ".join(f"{k} ×{v}" if v > 1 else k for k, v in tally.items())
    rows.sort(key=lambda r: (r["row"] is None, r["row"] or 0))
    counts = {k: 0 for k, _ in COMPARE_STATUSES}
    for r in rows:
        counts[r["status"]] += 1
    counts["duplicates"] = len(cleaned["duplicates"])
    counts["total"] = cleaned["total"]
    counts["name_warn"] = sum(1 for r in rows if r["name_warn"])
    counts["filtered"] = filtered
    return rows, counts


def benefit_details_text(matched, limit=None):
    items = matched[:limit] if limit else matched
    text = "؛ ".join(
        f'{h["aid_type__name"]} — {h["receipt_date"].isoformat()} — {h["project__name"]}' for h in items
    )
    if limit and len(matched) > limit:
        text += f" … (+{len(matched) - limit})"
    return text


# ---------------------------------------------------------------- المشروع والإحصاءات
def project_overview(project):
    qs = Benefit.objects.filter(project=project, beneficiary__is_active=True)
    return {
        "kashfs": Distribution.objects.filter(project=project).count(),
        "benefits": qs.count(),
        "people": qs.values("beneficiary").distinct().count(),
        "manual": qs.filter(source=Benefit.MANUAL).count(),
        "by_type": list(
            qs.values("aid_type__name").annotate(n=Count("id"), people=Count("beneficiary", distinct=True)).order_by("-n")
        ),
    }


def iter_project_rows(project, chunk=2000):
    """كل أسماء المشروع (كل كشوفاته) مع المصدر: اسم الملف أو إدخال يدوي."""
    qs = (
        Benefit.objects.filter(project=project, beneficiary__is_active=True)
        .select_related("beneficiary", "aid_type", "distribution__upload")
        .order_by("receipt_date", "aid_type__name", "beneficiary__name_search", "id")
    )
    for b in qs.iterator(chunk_size=chunk):
        yield b


def home_stats(scope):
    total = visible_beneficiaries(scope).count()
    served = scoped_benefits(scope).filter(beneficiary__is_active=True).values("beneficiary").distinct().count()
    return {
        "beneficiaries": total, "served": served, "not_served": total - served,
        "benefits": scoped_benefits(scope).count(),
        "distributions": Distribution.objects.filter(scope_q(scope), names_count__gt=0).count(),
    }


def overview_stats(scope=None):
    active = scoped_benefits(scope).filter(beneficiary__is_active=True)
    total = visible_beneficiaries(scope).count()
    served = active.values("beneficiary").distinct().count()
    by_type = list(active.values("aid_type__name").annotate(n=Count("id"), people=Count("beneficiary", distinct=True)).order_by("-n"))
    by_project = list(active.values("project__name").annotate(n=Count("id"), people=Count("beneficiary", distinct=True)).order_by("-n"))
    from django.db.models.functions import TruncMonth
    by_month = list(active.annotate(m=TruncMonth("receipt_date")).values("m").annotate(n=Count("id")).order_by("-m")[:12])
    by_month.reverse()
    freq = {}
    for r in active.values("beneficiary").annotate(n=Count("id")).values_list("n", flat=True).iterator(chunk_size=5000):
        k = r if r < 5 else 5
        freq[k] = freq.get(k, 0) + 1
    freq_rows = [(("5 فأكثر" if k == 5 else str(k)), freq[k]) for k in sorted(freq)]
    dup_names = list(
        visible_beneficiaries(scope).values("name_search").annotate(n=Count("id"))
        .filter(n__gt=1).order_by("-n")[:30]
    )
    return {
        "total": total, "served": served, "not_served": total - served,
        "benefits": active.count(), "distributions": Distribution.objects.filter(scope_q(scope)).count(),
        "by_type": by_type, "by_project": by_project, "by_month": by_month, "freq": freq_rows,
        "dup_names": dup_names, "alias_count": NameAlias.objects.filter(beneficiary__in=visible_beneficiaries(scope)).count(),
        "manual": active.filter(source=Benefit.MANUAL).count(),
    }


def compare_names(table, mapping, cd):
    """مقارنة ملف فيه أسماء فقط (بلا هوية): مطابقة تامة للاسم بعد التوحيد.
    الاسم الذي يطابق أكثر من هوية يُعاد «يحتاج مراجعة» ولا يُخمَّن."""
    from .textutils import clean_text, is_blank
    col = mapping["name"]
    wanted, order = {}, []
    rejected, dups = [], []
    total = 0
    for rec in table.to_dict("records"):
        raw = rec.get(col)
        if is_blank(raw):
            continue
        total += 1
        name = clean_text(raw)
        key = normalize_arabic(name)
        if key in wanted:
            dups.append(key)
            continue
        wanted[key] = {"row": rec.get("__row__"), "name": name}
        order.append(key)
    found = {}
    for part in _chunks(order):
        for b in visible_beneficiaries(cd.get("scope")).filter(name_search__in=part):
            found.setdefault(b.name_search, []).append(b)
    valid = []
    for key in order:
        item, matches = wanted[key], found.get(key, [])
        if len(matches) == 1:
            valid.append({"row": item["row"], "national_id": matches[0].national_id, "name": item["name"]})
        elif len(matches) > 1:
            rejected.append({"row": item["row"], "national_id": "", "name": item["name"],
                             "reason": f"الاسم يطابق {len(matches)} هويات مختلفة؛ استخدم رقم الهوية"})
        else:
            valid.append({"row": item["row"], "national_id": "", "name": item["name"]})
    cleaned = {"valid": valid, "rejected": rejected, "duplicates": dups, "total": total}
    return compare_file(cleaned, cd, name_given=False)
