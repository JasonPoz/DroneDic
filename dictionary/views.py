from difflib import get_close_matches
import json
import re
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from django import forms
from django.db.models import Q, Prefetch, Case, When, Value, IntegerField
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.forms import UserCreationForm
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django.views.generic import ListView
from urllib.parse import quote_plus

from .models import (
    TermTranslation,
    Definition,
    Context,
    Language,
    DroneTerm,
    SearchQuery,
)

User = get_user_model()

class RegistrationForm(UserCreationForm):
    """
    Форма регистрации пользователя с обязательным email.
    """
    email = forms.EmailField(required=True, label="Email")

    class Meta:
        model = User
        fields = ("username", "email", "password1", "password2")

    def clean_email(self):
        email = self.cleaned_data.get("email")

        if email and User.objects.filter(email=email).exists():
            raise forms.ValidationError("Пользователь с таким email уже существует.")

        return email


def _lang_fields_for_legacy(lang_code: str):
    """
    Возвращает имена полей legacy-модели DroneTerm для заданного языка.
    Поддерживаются только ru и en.
    """
    mapping = {
        "ru": {
            "term": "term_rus",
            "abbr": "abbr_rus",
            "definition": "definition_rus",
            "context": "context_rus",
        },
        "en": {
            "term": "term_eng",
            "abbr": "abbr_eng",
            "definition": "definition_eng",
            "context": "context_eng",
        },
    }
    return mapping.get(lang_code)


def _term_payload(name):
    if not name:
        return None
    return {"name": name}

def _strip_urls(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'https?://\S+|www\.\S+', '', text, flags=re.IGNORECASE).strip()

def _query_words(query_text: str):
    """
    Разбивает запрос на отдельные слова.
    Используется для поиска без учета порядка слов.
    """
    return [
        word.lower()
        for word in re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]+", query_text or "")
        if len(word) > 1 or word.isdigit()
    ]


def _contains_all_query_words(text: str, query_text: str) -> bool:
    """
    Проверяет, что все слова из запроса присутствуют в тексте,
    даже если порядок слов отличается.
    """
    cleaned_text = _strip_urls(text).lower()
    words = _query_words(query_text)

    if not words:
        return False

    return all(word in cleaned_text for word in words)


def _build_all_words_q(words, fields):
    """
    Формирует Q-запрос Django:
    каждое слово должно встретиться хотя бы в одном из указанных полей.
    Например: "системы беспилотные" найдет "беспилотные системы".
    """
    result_q = Q()

    for word in words:
        word_q = Q()

        for field in fields:
            word_q |= Q(**{f"{field}__icontains": word})

        result_q &= word_q

    return result_q


def _contains_query_without_urls(text: str, query_text: str) -> bool:
    cleaned_text = _strip_urls(text).lower()
    cleaned_query = (query_text or "").strip().lower()
    return bool(cleaned_query) and cleaned_query in cleaned_text

def _build_rows_from_translations(source_translations, source_lang_code: str, target_lang_code: str):
    rows = []

    for src_tr in source_translations:
        term = src_tr.term

        tgt_tr = (
            TermTranslation.objects
            .filter(term=term, language__code=target_lang_code)
            .first()
        )

        def_src = (
            Definition.objects
            .filter(term=term, language__code=source_lang_code)
            .first()
        )
        def_tgt = (
            Definition.objects
            .filter(term=term, language__code=target_lang_code)
            .first()
        )

        ctx_src = (
            Context.objects
            .filter(term=term, language__code=source_lang_code)
            .first()
        )
        ctx_tgt = (
            Context.objects
            .filter(term=term, language__code=target_lang_code)
            .first()
        )

        rows.append({
            "src_term": {"name": src_tr.name},
            "tgt_term": {"name": tgt_tr.name} if tgt_tr else None,
            "src_def": def_src.text if def_src else "",
            "tgt_def": def_tgt.text if def_tgt else "",
            "src_ctx": ctx_src.text if ctx_src else "",
            "tgt_ctx": ctx_tgt.text if ctx_tgt else "",
        })

    return rows


def _search_normalized_schema(query_text: str, source_lang_code: str):
    """
    Поиск по новой схеме:
    - название термина
    - аббревиатура
    - определение (без учета URL)
    - контекст (без учета URL)
    """
    base_qs = (
        TermTranslation.objects
        .filter(language__code=source_lang_code)
        .select_related("term", "language")
        .prefetch_related("term__definitions__language", "term__contexts__language")
        .distinct()
    )

    # 1. Сначала ищем только по названию и аббревиатуре
    direct_name_abbr = (
        base_qs.filter(
            Q(name__icontains=query_text)
            | Q(abbreviation__icontains=query_text)
        )
        .annotate(
            match_rank=Case(
                When(name__icontains=query_text, then=Value(4)),
                When(abbreviation__icontains=query_text, then=Value(3)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by("-match_rank", "name")
        .distinct()
    )

    if direct_name_abbr.exists():
        return direct_name_abbr
            # 1.1. Поиск по словам без учета порядка
    query_words = _query_words(query_text)

    if len(query_words) > 1:
        unordered_name_abbr = (
            base_qs.filter(
                _build_all_words_q(query_words, ["name", "abbreviation"])
            )
            .annotate(
                match_rank=Value(2, output_field=IntegerField())
            )
            .order_by("-match_rank", "name")
            .distinct()
        )

        if unordered_name_abbr.exists():
            return unordered_name_abbr

    # 2. Затем ищем по определениям и контексту, но уже без URL
    matched_ids = []

    for tr in base_qs:
        definitions = [
            d.text for d in tr.term.definitions.all()
            if d.language.code == source_lang_code
        ]
        contexts = [
            c.text for c in tr.term.contexts.all()
            if c.language.code == source_lang_code
        ]

        definition_match = any(
            _contains_query_without_urls(text, query_text)
            or _contains_all_query_words(text, query_text)
            for text in definitions
        )

        context_match = any(
            _contains_query_without_urls(text, query_text)
            or _contains_all_query_words(text, query_text)
            for text in contexts
        )

        if definition_match or context_match:
            matched_ids.append(tr.id)

    if matched_ids:
        return (
            base_qs.filter(id__in=matched_ids)
            .order_by("name")
            .distinct()
        )

    # 3. fuzzy только по названию и аббревиатуре
    names = list(base_qs.values_list("name", flat=True))
    abbreviations = [abbr for abbr in base_qs.values_list("abbreviation", flat=True) if abbr]
    fuzzy_candidates = list(dict.fromkeys(names + abbreviations))

    close_matches = get_close_matches(query_text, fuzzy_candidates, n=50, cutoff=0.6)
    if not close_matches and len(query_text) > 2:
        close_matches = get_close_matches(query_text[:-1], fuzzy_candidates, n=50, cutoff=0.6)

    if not close_matches:
        return TermTranslation.objects.none()

    return (
        base_qs.filter(Q(name__in=close_matches) | Q(abbreviation__in=close_matches))
        .order_by("name")
        .distinct()
    )


def _search_legacy_schema(query_text: str, source_lang_code: str, target_lang_code: str):
    """
    Поиск по старой схеме DroneTerm:
    - термин
    - аббревиатура
    - определение (без учета URL)
    - контекст (без учета URL)
    """
    source_fields = _lang_fields_for_legacy(source_lang_code)
    target_fields = _lang_fields_for_legacy(target_lang_code)

    if not source_fields:
        return []

    term_field = source_fields["term"]
    abbr_field = source_fields["abbr"]
    definition_field = source_fields["definition"]
    context_field = source_fields["context"]

    # 1. Сначала поиск по названию и аббревиатуре
    qs = (
        DroneTerm.objects.filter(
            Q(**{f"{term_field}__icontains": query_text})
            | Q(**{f"{abbr_field}__icontains": query_text})
        )
        .annotate(
            match_rank=Case(
                When(**{f"{term_field}__icontains": query_text}, then=Value(4)),
                When(**{f"{abbr_field}__icontains": query_text}, then=Value(3)),
                default=Value(0),
                output_field=IntegerField(),
            )
        )
        .order_by("-match_rank", term_field)
    )
    
        # 1.1. Поиск по словам без учета порядка в legacy-модели
    query_words = _query_words(query_text)

    if not qs.exists() and len(query_words) > 1:
        unordered_q = _build_all_words_q(query_words, [term_field, abbr_field])
        qs = DroneTerm.objects.filter(unordered_q).order_by(term_field)

    # 2. Если ничего не нашли — ищем по определению и контексту без URL
    if not qs.exists():
        matched_ids = []

        for item in DroneTerm.objects.all():
            definition_value = getattr(item, definition_field, "") or ""
            context_value = getattr(item, context_field, "") or ""

            if (
                _contains_query_without_urls(definition_value, query_text)
                or _contains_all_query_words(definition_value, query_text)
                or _contains_query_without_urls(context_value, query_text)
                or _contains_all_query_words(context_value, query_text)
            ):
                matched_ids.append(item.id)

        if matched_ids:
            qs = DroneTerm.objects.filter(id__in=matched_ids).order_by(term_field)

    # 3. Если и тут ничего — fuzzy по термину и аббревиатуре
    if not qs.exists():
        candidates = []
        for item in DroneTerm.objects.all():
            term_value = getattr(item, term_field, "") or ""
            abbr_value = getattr(item, abbr_field, "") or ""
            if term_value:
                candidates.append(term_value)
            if abbr_value:
                candidates.append(abbr_value)

        close_matches = get_close_matches(query_text, candidates, n=50, cutoff=0.6)
        if not close_matches and len(query_text) > 2:
            close_matches = get_close_matches(query_text[:-1], candidates, n=50, cutoff=0.6)

        if close_matches:
            qs = DroneTerm.objects.filter(
                Q(**{f"{term_field}__in": close_matches})
                | Q(**{f"{abbr_field}__in": close_matches})
            ).order_by(term_field)

    rows = []

    for item in qs:
        src_term = getattr(item, term_field, "") or ""
        src_def = getattr(item, definition_field, "") or ""
        src_ctx = getattr(item, context_field, "") or ""

        tgt_term = ""
        tgt_def = ""
        tgt_ctx = ""

        if target_fields:
            tgt_term = getattr(item, target_fields["term"], "") or ""
            tgt_def = getattr(item, target_fields["definition"], "") or ""
            tgt_ctx = getattr(item, target_fields["context"], "") or ""

        rows.append({
            "src_term": _term_payload(src_term),
            "tgt_term": _term_payload(tgt_term),
            "src_def": src_def,
            "tgt_def": tgt_def,
            "src_ctx": src_ctx,
            "tgt_ctx": tgt_ctx,
        })

    return rows

def _parse_gigatran_html(html: str, query_text: str, limit: int = 10):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    query_words = [w.strip().lower() for w in query_text.split() if w.strip()]

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)

        if not text:
            continue

        if "/article/?id=" not in href:
            continue

        full_url = href
        if href.startswith("/"):
            full_url = f"https://intent.gigatran.com{href}"

        if full_url in seen:
            continue

        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        combined_text = f"{text} {parent_text}".lower()

        # оставляем только относительно релевантные результаты
        if query_words and not any(word in combined_text for word in query_words):
            continue

        seen.add(full_url)

        definition = ""
        if parent_text and parent_text != text:
            definition = parent_text.replace(text, "").strip(" —-:;")

        results.append({
            "term": text,
            "definition": definition,
            "url": full_url,
            "source": "Gigatran",
        })

        if len(results) >= limit:
            break

    return results


def fetch_from_external_api(query_text: str, source_lang_code="ru", target_lang_code="en"):
    """
    Внешний поиск по Gigatran / Intent.
    Если сервис отдаёт JSON — используем его.
    Если отдаёт HTML — парсим ссылки на статьи.
    """
    if not query_text:
        return {"results": []}

    query_encoded = quote_plus(query_text)

    candidate_urls = [
        f"https://intent.gigatran.com/?query={query_encoded}",
        f"https://intent.gigatran.com/index/?query={query_encoded}",
        f"https://intent.gigatran.com/search/?query={query_encoded}",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BAS-Dictionary/1.0)"
    }

    last_error = None

    for url in candidate_urls:
        try:
            response = requests.get(url, headers=headers, timeout=8)
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower()

            # Если вдруг сервис отдаст JSON — используем его
            if "application/json" in content_type:
                try:
                    data = response.json()
                except ValueError:
                    continue

                if isinstance(data, dict):
                    results = data.get("results", [])
                elif isinstance(data, list):
                    results = data
                else:
                    results = []

                if results:
                    return {
                        "results": results,
                        "query": query_text,
                        "source_lang": source_lang_code,
                        "target_lang": target_lang_code,
                        "provider": "Gigatran",
                    }

            # Иначе пробуем HTML-парсинг
            if "text/html" in content_type or "application/xhtml+xml" in content_type or not content_type:
                results = _parse_gigatran_html(response.text, query_text=query_text, limit=10)
                if results:
                    return {
                        "results": results,
                        "query": query_text,
                        "source_lang": source_lang_code,
                        "target_lang": target_lang_code,
                        "provider": "Gigatran",
                    }

        except requests.RequestException as exc:
            last_error = str(exc)

    return {
        "results": [],
        "query": query_text,
        "source_lang": source_lang_code,
        "target_lang": target_lang_code,
        "provider": "Gigatran",
        "error": last_error or "Ничего не найдено во внешнем источнике",
    }


class TermSearchView(ListView):
    model = TermTranslation
    template_name = "pages/search_results.html"
    context_object_name = "source_translations"

    source_lang_code = "ru"
    target_lang_code = "en"
    query_text = ""
    _rows = None

    def get_queryset(self):
        self.query_text = (self.request.GET.get("q") or self.request.GET.get("query") or "").strip()
        self.source_lang_code = (
            self.request.GET.get("source_lang")
            or self.request.GET.get("lang")
            or "ru"
        ).strip()
        self.target_lang_code = (self.request.GET.get("target_lang") or "en").strip()
        self._rows = []

        if not self.query_text:
            return TermTranslation.objects.none()

        normalized_qs = _search_normalized_schema(self.query_text, self.source_lang_code)

        if normalized_qs.exists():
            self._rows = _build_rows_from_translations(
                normalized_qs,
                self.source_lang_code,
                self.target_lang_code,
            )
            return normalized_qs

        # fallback на legacy DroneTerm
        self._rows = _search_legacy_schema(
            self.query_text,
            self.source_lang_code,
            self.target_lang_code,
        )
        return TermTranslation.objects.none()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        source_lang = Language.objects.filter(code=self.source_lang_code).first()
        target_lang = Language.objects.filter(code=self.target_lang_code).first()
        extra_targets = Language.objects.exclude(
            code__in=[self.source_lang_code, self.target_lang_code]
        )

        rows = self._rows or []

        if self.request.user.is_authenticated and self.query_text:
            SearchQuery.objects.create(
                user=self.request.user,
                query=self.query_text,
                source_lang=self.source_lang_code,
                target_lang=self.target_lang_code,
                results_count=len(rows),
            )

        context.update({
            "rows": rows,
            "query": self.query_text,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "extra_targets": extra_targets,
        })
        return context


@require_GET
def external_search_results(request):
    query = (request.GET.get("query") or "").strip()
    source_lang = (request.GET.get("source_lang") or "ru").strip()
    target_lang = (request.GET.get("target_lang") or "en").strip()

    if not query:
        return JsonResponse(
            {"error": "Не передан параметр query", "results": []},
            status=400
        )

    data = fetch_from_external_api(
        query_text=query,
        source_lang_code=source_lang,
        target_lang_code=target_lang,
    )

    return JsonResponse(data, status=200)


class DictionaryView(ListView):
    model = TermTranslation
    template_name = "pages/dictionary.html"
    context_object_name = "translations"
    paginate_by = 30

    def get_queryset(self):
        lang_code = self.request.GET.get("lang", "ru").strip()
        defs_qs = Definition.objects.filter(language__code=lang_code)

        return (
            TermTranslation.objects
            .filter(language__code=lang_code)
            .select_related("term", "language")
            .prefetch_related(
                Prefetch("term__definitions", queryset=defs_qs, to_attr="defs_for_lang")
            )
            .order_by("name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        lang_code = self.request.GET.get("lang", "ru").strip()

        context.update({
            "language": Language.objects.filter(code=lang_code).first(),
            "lang_code": lang_code,
            "languages": Language.objects.all().order_by("code"),
        })
        return context


def history_view(request):
    if not request.user.is_authenticated:
        return redirect("login")

    history = (
        SearchQuery.objects
        .filter(user=request.user)
        .order_by("-created_at")
    )

    return render(request, "pages/history.html", {
        "history": history,
        "items": history,
    })


@require_GET
def autocomplete_terms(request):
    term = (request.GET.get("term") or "").strip()
    lang_code = (request.GET.get("source_lang") or request.GET.get("lang") or "ru").strip()

    results = []

    # Новая схема
    normalized = list(
        TermTranslation.objects
        .filter(language__code=lang_code, name__icontains=term)
        .values_list("name", flat=True)[:10]
    )
    results.extend(normalized)

    # Legacy-схема
    legacy_fields = _lang_fields_for_legacy(lang_code)
    if legacy_fields:
        term_field = legacy_fields["term"]
        abbr_field = legacy_fields["abbr"]

        legacy_terms = list(
            DroneTerm.objects
            .filter(
                Q(**{f"{term_field}__icontains": term})
                | Q(**{f"{abbr_field}__icontains": term})
            )
            .values_list(term_field, flat=True)[:10]
        )
        results.extend([value for value in legacy_terms if value])

    unique_results = list(dict.fromkeys(results))[:10]
    return JsonResponse(unique_results, safe=False)


@csrf_exempt
def translate_term(request):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=400)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    term_id = data.get("term_id")
    target_lang = data.get("target_lang")

    if not term_id or not target_lang:
        return JsonResponse({"error": "term_id and target_lang are required"}, status=400)

    try:
        src_translation = (
            TermTranslation.objects
            .select_related("term", "language")
            .get(id=term_id)
        )
    except TermTranslation.DoesNotExist:
        return JsonResponse({"error": "Term not found"}, status=404)

    src_lang_code = src_translation.language.code

    target_translation = (
        TermTranslation.objects
        .filter(term=src_translation.term, language__code=target_lang)
        .first()
    )

    if target_translation:
        term_translated = target_translation.name
    else:
        try:
            term_translated = GoogleTranslator(
                source=src_lang_code,
                target=target_lang
            ).translate(src_translation.name)
        except Exception:
            term_translated = ""

    def_src_obj = Definition.objects.filter(
        term=src_translation.term,
        language__code=src_lang_code
    ).first()
    definition_source = def_src_obj.text if def_src_obj else ""

    def_tgt_obj = Definition.objects.filter(
        term=src_translation.term,
        language__code=target_lang
    ).first()

    if def_tgt_obj:
        definition_translated = def_tgt_obj.text
    elif definition_source:
        try:
            definition_translated = GoogleTranslator(
                source=src_lang_code,
                target=target_lang
            ).translate(definition_source)
        except Exception:
            definition_translated = ""
    else:
        definition_translated = ""

    ctx_src_obj = Context.objects.filter(
        term=src_translation.term,
        language__code=src_lang_code
    ).first()
    context_source = ctx_src_obj.text if ctx_src_obj else ""

    ctx_tgt_obj = Context.objects.filter(
        term=src_translation.term,
        language__code=target_lang
    ).first()

    if ctx_tgt_obj:
        context_translated = ctx_tgt_obj.text
    elif context_source:
        try:
            context_translated = GoogleTranslator(
                source=src_lang_code,
                target=target_lang
            ).translate(context_source)
        except Exception:
            context_translated = ""
    else:
        context_translated = ""

    source_lang_name = (
        Language.objects
        .filter(code=src_lang_code)
        .values_list("name", flat=True)
        .first() or src_lang_code
    )
    target_lang_name = (
        Language.objects
        .filter(code=target_lang)
        .values_list("name", flat=True)
        .first() or target_lang
    )

    return JsonResponse({
        "term_source": src_translation.name,
        "term_translated": term_translated,
        "source_lang": src_lang_code,
        "source_lang_name": source_lang_name,
        "target_lang": target_lang,
        "target_lang_name": target_lang_name,
        "definition_source": definition_source,
        "definition_translated": definition_translated,
        "context_source": context_source,
        "context_translated": context_translated,
    })


def registration_view(request):
    """
    Регистрация нового пользователя.

    Если форма заполнена корректно, пользователь сохраняется,
    автоматически авторизуется и перенаправляется на главную страницу.
    Если есть ошибки, они возвращаются обратно в шаблон и отображаются пользователю.
    """
    if request.user.is_authenticated:
        return redirect("dictionary:home")

    if request.method == "POST":
        form = RegistrationForm(request.POST)

        if form.is_valid():
            user = form.save(commit=False)
            user.email = form.cleaned_data["email"]
            user.save()

            login(request, user)
            messages.success(request, "Регистрация прошла успешно.")
            return redirect("dictionary:home")

        messages.error(request, "Проверьте правильность заполнения формы.")
    else:
        form = RegistrationForm()

    return render(request, "registration/registration-form.html", {
        "form": form
    })

def home(request):
    return render(request, "pages/home.html")
