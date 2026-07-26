---
name: hatch-pet
description: Create, repair, validate, QA, and install LimeBot-compatible v2 animated pets from concepts, reference art, existing spritesheets, or Codex pet packages. Use for 8x11 atlases, 9 standard animation rows, 16 look directions, transparent WEBP packaging, or dashboard and browser-companion pet integration.
---

# Hatch Pet

Create validated animated pets for LimeBot's web dashboard and browser companion. The skill accepts a reference image or an existing atlas, uses the native image-generation capability for new visual work, and uses the bundled deterministic scripts for extraction, assembly, transparency cleanup, validation, and QA.

## Product contract

Every installable pet is a v2 atlas:

- `1536x2288` RGBA WEBP, 8 columns by 11 rows, each cell `192x208`.
- Rows `0-8` are, in order: `idle`, `running-right`, `running-left`, `waving`, `jumping`, `failed`, `waiting`, `running`, and `review`.
- Row `9` is `000, 022.5, 045, 067.5, 090, 112.5, 135, 157.5`.
- Row `10` is `180, 202.5, 225, 247.5, 270, 292.5, 315, 337.5`.
- `000` means looking up; `090` is screen-right; `180` is down; `270` is screen-left. Neutral/front is not one of the 16 look directions.
- The package metadata must contain `spriteVersionNumber: 2`.
- Transparent pixels must have cleared RGB residue, and the final atlas must pass `validate_atlas.py --require-v2`.

Keep the character identity, proportions, palette, materials, props, baseline, and visual style consistent across every row. Use one coherent row strip for a repaired or generated look row; never patch a single newly generated look cell beside unrelated cells.

## Workflow

1. Choose the pet id, display name, description, reference art, style, and a temporary run directory. Keep temporary work under `temp/hatch-pet/<id>` or another user-approved workspace path.
2. Prepare the run with `prepare_pet_run.py`. For a new visual, call LimeBot's native `generate_image` capability and use the generated base as the canonical reference for every row. For an existing atlas, preserve approved rows after structural and visual inspection.
3. Extract and inspect each standard row immediately. Fix deterministic extraction or chroma problems with the scripts before asking for new imagery. Do not use a bare system image API or a hand-written tiling script.
4. Write a short `qa/look-mechanics.md` decision before the look rows. Generate and approve the four cardinal anchors together, then generate row 9 as one coherent eight-pose family. Register and validate row 9 before generating row 10. Keep feet/lower torso or the natural grounded part anchored; let eyes, eyelids, head/neck, upper body, hair, ears, and props follow naturally.
5. Assemble the complete v2 atlas, run the single final chroma-despill pass, validate it, create the contact/direction/continuity QA artifacts, and resolve blind-review warnings using labeled normal-size review. A wrong or ambiguous cardinal, wrong quadrant, visible reversal, clipping, interior hole, scale pop, identity drift, or failed deterministic validation requires repair.
6. Package the approved asset into LimeBot's static pet catalog and, when requested, the browser companion catalog. Report the package paths and QA results.

## Deterministic scripts

Run scripts with LimeBot's active Python. `{baseDir}` is replaced with this skill directory by the skill loader. Pillow is required; if it is unavailable, report that requirement and stop rather than switching to another image-processing path.

```text
python {baseDir}/scripts/prepare_pet_run.py --pet-name "<Name>" --description "<one sentence>" --reference "<absolute image path>" --output-dir "<run path>" --pet-notes "<stable visual description>"
python {baseDir}/scripts/extract_strip_frames.py --decoded-dir "<run>/decoded" --output-dir "<run>/frames" --states all --method auto
python {baseDir}/scripts/inspect_frames.py --frames-root "<run>/frames" --json-out "<run>/qa/review.json" --require-components
python {baseDir}/scripts/compose_atlas.py --frames-root "<run>/frames" --output "<run>/final/spritesheet.png" --webp-output "<run>/final/spritesheet.webp"
python {baseDir}/scripts/assemble_extended_atlas.py --base-atlas "<run>/final/spritesheet.webp" --look-row-9 "<run>/decoded/look-row-9.png" --look-row-10 "<run>/decoded/look-row-10.png" --neutral-cell "<run>/frames/idle/00.png" --chroma-key "<hex key>" --output "<run>/final/spritesheet-extended.png" --webp-output "<run>/final/spritesheet-extended.webp" --manifest-output "<run>/final/spritesheet-extended.json"
python {baseDir}/scripts/despill_chroma_edges.py "<run>/final/spritesheet-extended.png" --output "<run>/final/spritesheet-extended.png" --webp-output "<run>/final/spritesheet-extended.webp" --chroma-key "<hex key>" --json-out "<run>/qa/chroma-despill-extended.json"
python {baseDir}/scripts/validate_atlas.py "<run>/final/spritesheet-extended.webp" --json-out "<run>/final/validation-extended.json" --chroma-key "<hex key>" --require-v2
python {baseDir}/scripts/make_contact_sheet.py "<run>/final/spritesheet-extended.webp" --output "<run>/qa/contact-sheet-extended.png"
python {baseDir}/scripts/make_direction_qa_sheet.py "<run>/final/spritesheet-extended.webp" --output "<run>/qa/look-directions.png"
python {baseDir}/scripts/measure_direction_continuity.py "<run>/final/spritesheet-extended.webp" --json-out "<run>/qa/look-continuity.json"
```

Use the remaining bundled scripts for cardinal extraction, stable-slot extraction, running-left derivation, motion previews, blind-direction consensus, and blind-direction validation. Read the relevant reference before using an unfamiliar script:

- `references/codex-pet-contract.md` — package and atlas contract.
- `references/animation-rows.md` — state meanings and row semantics.
- `references/qa-rubric.md` — structural, visual, and direction acceptance criteria.

## LimeBot installation layout

Install a pet as a static client asset; do not put binary atlas data in `persona/IDENTITY.md` or the conversation history. The dashboard catalog is:

```text
web/public/pets/<pet-id>/
  spritesheet.webp
  pet.json
  manifest.json
web/public/pets/index.json
```

The browser companion uses the same manifest shape under `extension/public/pets/<pet-id>/` and `extension/public/pets/index.json`. The manifest must include:

```json
{
  "id": "crimson",
  "displayName": "Crimson",
  "spriteVersionNumber": 2,
  "spritesheetPath": "/pets/crimson/spritesheet.webp",
  "frameWidth": 192,
  "frameHeight": 208,
  "columns": 8,
  "rows": 11,
  "fps": 8,
  "stateRows": {
    "idle": 0,
    "running-right": 1,
    "running-left": 2,
    "waving": 3,
    "jumping": 4,
    "failed": 5,
    "waiting": 6,
    "running": 7,
    "review": 8
  },
  "stateFrameCounts": {
    "idle": 6,
    "running-right": 8,
    "running-left": 8,
    "waving": 4,
    "jumping": 5,
    "failed": 8,
    "waiting": 6,
    "running": 6,
    "review": 6
  },
  "stateFrameDurations": {
    "idle": [280, 110, 110, 140, 140, 320],
    "running-right": [120, 120, 120, 120, 120, 120, 120, 220],
    "running-left": [120, 120, 120, 120, 120, 120, 120, 220],
    "waving": [140, 140, 140, 280],
    "jumping": [140, 140, 140, 140, 280],
    "failed": [140, 140, 140, 140, 140, 140, 140, 240],
    "waiting": [150, 150, 150, 150, 150, 260],
    "running": [120, 120, 120, 120, 120, 220],
    "review": [150, 150, 150, 150, 150, 280]
  },
  "lookRows": { "upper": 9, "lower": 10 }
}
```

Update the catalog index when adding a pet. Preserve the current custom-avatar fallback: a configured persona avatar may continue to render as a static avatar, while a missing avatar uses the selected animated pet. The first built-in pet is `crimson`.

Map runtime state conservatively:

- disconnected → `idle` or a static fallback
- typing/thinking → `running`
- active tool work → `running`
- approval request → `waiting`
- error/rate limit → `failed`
- completed success → `waving` or `jumping`

Only add new backend/WebSocket event fields when the requested behavior cannot be derived from existing connection, typing, tool, approval, and message state. A dashboard or extension pet does not require changes to Discord, WhatsApp, Telegram, the agent loop, or the message bus.

## Safety and handoff

Use LimeBot's canonical filesystem tools for user files and `run_command` only for the deterministic scripts. Respect `ALLOWED_PATHS`, request confirmation for writes, keep source art and generated intermediates separate from the installed catalog, and never expose secrets in manifests or QA reports. Do not create a desktop-wide always-on-top overlay unless the user explicitly requests a separate desktop host.

Before reporting success, include:

- installed pet id and catalog paths;
- `pet.json` and manifest paths;
- atlas dimensions, format, and `spriteVersionNumber`;
- validation/chroma result and any accepted minor warnings;
- the dashboard and extension surfaces tested.
