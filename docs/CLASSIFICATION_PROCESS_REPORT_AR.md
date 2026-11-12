# تقرير: تطبيق الـClassification Process على `New_relationships`

> كل الشغل من غير API (mock backend + شبكة مقفولة). مفيش commit ولا push.

## 1. الـprocess على الكود

| # | الخطوة | الحالة | الكود |
|---|---|---|---|
| 1 | فتح الكتالوج (نص + رسومات) | ✅ موجود | `source.py` |
| 2 | البحث عن الجداول | ✅ موجود | Stage 1 `tables` + `page_kind` |
| 3 | `profile_id` من الجدول | ✅ موجود | `rules.article_column` + `exports` |
| 4 | bbox للتفصيلة والقطع | ✅ موجود | `Detail.bbox` / `Node.bbox` |
| 5 | `profile_id` من الـcallout | ✅ موجود | `Node.article_id` / `callout` |
| 6 | نوع الرسمة + التوجيه | ✅ **اتضاف التوجيه** | `rules.view_feeds`: `profile_2d`/`table` → Article DB بس؛ `assembly_*` → Assembly DB. + `ClassifyDrawing` (DSPy) لو النوع مش معروف |
| 7 | كل معلومة عن الـarticle في الكتالوج | ✅ موجود | `articles.build_index` / `link` |
| 8 | FWR: Bar / Piece / Parameter | ✅ **اتصلح** | ملف المعرفة: spacer = piece، sealing tape = parameter، gaskets = parameter + `ClassifyFWR` (DSPy) للقطع اللي المعرفة ما تعرفهاش |
| 9 | spacing / edge_spacing / depth للـpieces | ✅ **اتضاف** | `assembly._piece_only`: القيمة لـbar/parameter بتتنقل لـ`other` (ALL ELSE FOUND) + `ExtractPieceSpacing` (DSPy) للـpieces الناقصة |
| — | **الـdeliverables** | ✅ **جديد** | `data/article_db.json` + `data/assembly_db.json` (`deliverables.py`) |

## 2. الـdeliverables (الـoutput النهائي)

**`data/article_db.json`**: سجل واحد لكل `profile_id` في الكتالوج كله، بالترتيب:
`profile_id, profile_role, role_clarification, article_type (FWR), depth, edge_spacing, spacing, quantity, material, finish, color, other (ALL ELSE FOUND)`
+ `occurrences` (كل صفحة/تفصيلة ظهر فيها) + `sources` + `conflicts` + `missing_information`.
- القيم المختلفة بين الصفحات **ما بتتحسمش**: أول قيمة صريحة بتتاخد، والباقي في `conflicts` بالصفحة.
- القطعة من غير رقم ما ينفعش تتربط بين التفاصيل، فبتفضل سجل لوحدها.

**`data/assembly_db.json`**: كل علاقة متحقق منها: `relation` (clamped / bonded / fixed / supported / movement / insertion / other + clarification)، `fastener_assigned` لـ`fixed` بس، + الباقي (participants، مسار المسمار، المسافات، الشروط، الدليل).

**قواعد بيتحقق منها الكود** (`schema.validate_deliverables`): `profile_id` مش مكرر، spacing للـpieces بس، fastener لـfixed بس، other لازم توضيح، كل participant موجود في الـArticle DB.

الـschemas: `schemas/article_db.schema.json` و`schemas/assembly_db.schema.json`.

## 3. DSPy (`facade_dspy/dspy_steps.py`)

| Signature | الخطوة | Reward (حتمي، الموديل ما بيقيّمش نفسه) |
|---|---|---|
| `ClassifyDrawing` | 6 | النوع من القائمة + سبب |
| `ClassifyFWR` | 8 | bar/piece/parameter + سبب |
| `ExtractPieceSpacing` | 9 | **كل رقم مكتوب في الاقتباس، والاقتباس موجود في نص الصفحة**؛ dimension line لازم معاه offset |
| `ClassifyRelation` | Assembly | القائمة المحددة؛ other لازم توضيح؛ fastener لـfixed بس |

- كل خطوة `dspy.BestOfN(N=2)` بالـreward؛ الإجابة **بتتقبل بس لو reward = 1**، وبتتعلّم `visual` + اسم الخطوة في `inference_reason`. الإجابة المرفوضة **ما بتغيّرش حاجة**.
- `BackendAdapter` بيبعت كل نداء على نفس الـbackend والميزانية بتاعة المشروع (زي `refine_extraction.py`).
- التشغيل على run محفوظ:
  ```text
  python -m facade_dspy.dspy_steps <run أو snapshot> --out <فولدر جديد> --budget-usd 1 --max-calls 50
  ```
  بيطلّع `data/article_db.json` و`data/assembly_db.json` و`data/detail_outputs.json` و`dspy_steps_log.json`.

## 4. الاختبارات

- **251 نجحوا، 6 skipped، صفر فشل.**
- جديد: `tests/test_dspy_steps.py` (5) + 4 في `tests/test_relationship_pipeline.py`:
  ترتيب حقول الـdeliverables، `profile_id` مش مكرر، spacing للـpieces بس، fastener لـfixed بس، `profile_2d` ما بيطلّعش علاقات، FWR حسب الـprocess، رفض قيمة مش مكتوبة، رفض fastener على علاقة مش fixed، تشغيل الأمر على run محفوظ.
- run قديم بيكمل بالكود الجديد بـ**صفر** نداءات للموديل.

## 5. ملفات اتعدّلت

`schema.py`، `rules.py`، `assembly.py`، `exports.py`، `contract_files.py`، `checkpoint_compat.py`، `knowledge/system_profiles.json`، وتيستين قدام.
جديد: `deliverables.py`، `dspy_steps.py`، `tests/test_dspy_steps.py`، `schemas/article_db.schema.json`، `schemas/assembly_db.schema.json`.

## 6. ملاحظات

- `index_dspy_pilot.py` **ما اتلمسش**، ولسه بيطلّع شكل مختلف (parts/interfaces). الـdeliverables الرسمية بقت `article_db.json` و`assembly_db.json` من الـpipeline الأساسي.
- `schema.py` على الجهاز كان فيه line endings مختلطة (CRLF وLF)؛ اتسجلت بصمته القديمة في `checkpoint_compat.py` علشان الـruns القديمة ما تتعادش.
- الـruns القديمة مفيهاش الحقول اللي اتضافت للـprompt مؤخرًا؛ علشان تتملا لازم تتقرا تاني أو تشغّل `dspy_steps` عليها (الاتنين محتاجين API).

## 7. الخطوة الجاية

1. Codex يراجع ويشغّل الاختبارات على Windows.
2. تشغيل حقيقي صغير: `python -m facade_dspy --target-pages 42,71,72 "data/ALUPCO50SG CAT.(3rd ED 2011).pdf" --budget-usd …` ثم `python -m facade_dspy.dspy_steps …` على النتيجة.
3. سؤال الفريق (أحمد / معتز / تيسير) عن أي deliverables زيادة؛ لحد ما يردوا بتتحط في `other`.
