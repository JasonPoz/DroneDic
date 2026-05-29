import sqlite3
from dictionary.models import (
    Language, Category, PartOfSpeech,
    Term, TermTranslation, Definition, Context
)

OLD_DB_PATH = "/home/c/co811774/public_html/db_source.sqlite3"

conn = sqlite3.connect(OLD_DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Языки должны уже существовать
lang_ru = Language.objects.get(code="ru")
lang_en = Language.objects.get(code="en")

rows = cur.execute("SELECT * FROM dictionary_droneterm").fetchall()

created_terms = 0
created_translations = 0
created_definitions = 0
created_contexts = 0

for row in rows:
    category_name = (row["category"] or "").strip()
    pos_name = (row["part_references"] or "").strip()

    category_obj = None
    if category_name:
        category_obj, _ = Category.objects.get_or_create(name=category_name)

    pos_obj = None
    if pos_name:
        pos_obj, _ = PartOfSpeech.objects.get_or_create(name=pos_name)

    # создаём новый Term под каждую старую запись
    term = Term.objects.create(
        category=category_obj,
        part_of_speech=pos_obj,
    )
    created_terms += 1

    term_rus = (row["term_rus"] or "").strip()
    abbr_rus = (row["abbr_rus"] or "").strip()
    term_eng = (row["term_eng"] or "").strip()
    abbr_eng = (row["abbr_eng"] or "").strip()

    if term_rus:
        TermTranslation.objects.create(
            term=term,
            language=lang_ru,
            name=term_rus,
            abbreviation=abbr_rus or None,
        )
        created_translations += 1

    if term_eng:
        TermTranslation.objects.create(
            term=term,
            language=lang_en,
            name=term_eng,
            abbreviation=abbr_eng or None,
        )
        created_translations += 1

    definition_rus = (row["definition_rus"] or "").strip()
    definition_eng = (row["definition_eng"] or "").strip()
    context_rus = (row["context_rus"] or "").strip()
    context_eng = (row["context_eng"] or "").strip()

    if definition_rus:
        Definition.objects.create(
            term=term,
            language=lang_ru,
            text=definition_rus,
        )
        created_definitions += 1

    if definition_eng:
        Definition.objects.create(
            term=term,
            language=lang_en,
            text=definition_eng,
        )
        created_definitions += 1

    if context_rus:
        Context.objects.create(
            term=term,
            language=lang_ru,
            text=context_rus,
        )
        created_contexts += 1

    if context_eng:
        Context.objects.create(
            term=term,
            language=lang_en,
            text=context_eng,
        )
        created_contexts += 1

print(f"Imported old rows: {len(rows)}")
print(f"Created Terms: {created_terms}")
print(f"Created TermTranslations: {created_translations}")
print(f"Created Definitions: {created_definitions}")
print(f"Created Contexts: {created_contexts}")