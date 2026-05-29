from django.core.management.base import BaseCommand

from deep_translator import GoogleTranslator



from dictionary.models import Language, TermTranslation, Definition, Context





TARGET_LANGS = [

    ("fr", "Французский", "fr"),

    ("es", "Испанский", "es"),

    ("cn", "Китайский", "zh-CN"),

]





class Command(BaseCommand):

    help = "Заполняет недостающие переводы терминов, определений и контекстов"



    def add_arguments(self, parser):

        parser.add_argument(

            "--source",

            default="ru",

            help="Исходный язык для перевода: ru или en"

        )



    def handle(self, *args, **options):

        source_code = options["source"]



        source_lang = Language.objects.filter(code=source_code).first()



        if not source_lang:

            self.stdout.write(self.style.ERROR(f"Исходный язык {source_code} не найден"))

            return



        source_translations = (

            TermTranslation.objects

            .filter(language=source_lang)

            .select_related("term", "language")

        )



        self.stdout.write(f"Исходный язык: {source_lang.name} ({source_lang.code})")

        self.stdout.write(f"Найдено исходных терминов: {source_translations.count()}")



        for code, name, translator_code in TARGET_LANGS:

            target_lang, _ = Language.objects.get_or_create(

                code=code,

                defaults={"name": name}

            )



            self.stdout.write("")

            self.stdout.write(f"Заполнение языка: {target_lang.name} ({target_lang.code})")



            translator = GoogleTranslator(

                source=source_code,

                target=translator_code

            )



            created_terms = 0

            created_defs = 0

            created_contexts = 0



            for src_tr in source_translations:

                term = src_tr.term



                # Перевод названия термина

                if not TermTranslation.objects.filter(term=term, language=target_lang).exists():

                    try:

                        translated_name = translator.translate(src_tr.name)

                    except Exception as exc:

                        self.stdout.write(self.style.WARNING(f"Ошибка перевода термина '{src_tr.name}': {exc}"))

                        translated_name = ""



                    if translated_name:

                        TermTranslation.objects.create(

                            term=term,

                            language=target_lang,

                            name=translated_name,

                            abbreviation=src_tr.abbreviation or "",

                            transliteration="",

                            author=src_tr.author,

                        )

                        created_terms += 1



                # Перевод определения

                src_def = Definition.objects.filter(

                    term=term,

                    language=source_lang

                ).first()



                if src_def and src_def.text:

                    if not Definition.objects.filter(term=term, language=target_lang).exists():

                        try:

                            translated_def = translator.translate(src_def.text)

                        except Exception as exc:

                            self.stdout.write(self.style.WARNING(f"Ошибка перевода определения: {exc}"))

                            translated_def = ""



                        if translated_def:

                            Definition.objects.create(

                                term=term,

                                language=target_lang,

                                text=translated_def,

                                author=src_def.author,

                            )

                            created_defs += 1



                # Перевод контекста

                src_ctx = Context.objects.filter(

                    term=term,

                    language=source_lang

                ).first()



                if src_ctx and src_ctx.text:

                    if not Context.objects.filter(term=term, language=target_lang).exists():

                        try:

                            translated_ctx = translator.translate(src_ctx.text)

                        except Exception as exc:

                            self.stdout.write(self.style.WARNING(f"Ошибка перевода контекста: {exc}"))

                            translated_ctx = ""



                        if translated_ctx:

                            Context.objects.create(

                                term=term,

                                language=target_lang,

                                text=translated_ctx,

                                author=src_ctx.author,

                            )

                            created_contexts += 1



            self.stdout.write(

                self.style.SUCCESS(

                    f"Готово для {code}: "

                    f"термины={created_terms}, "

                    f"определения={created_defs}, "

                    f"контексты={created_contexts}"

                )

            )
