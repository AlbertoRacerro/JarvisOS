import { RangeSetBuilder } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, type DecorationSet, type ViewUpdate } from "@codemirror/view";

/** Offsets (within a line) where each indentation guide starts: one per full `unit` spaces of leading whitespace. */
export function guideOffsets(line: string, unit = 4): number[] {
  let spaces = 0;
  while (spaces < line.length && line[spaces] === " ") spaces++;
  const offsets: number[] = [];
  for (let at = 0; at + unit <= spaces; at += unit) offsets.push(at);
  return offsets;
}

const guide = Decoration.mark({ class: "cm-indent-guide" });

function build(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  for (const { from, to } of view.visibleRanges) {
    for (let pos = from; pos <= to;) {
      const line = view.state.doc.lineAt(pos);
      for (const at of guideOffsets(line.text)) builder.add(line.from + at, line.from + at + 4, guide);
      pos = line.to + 1;
    }
  }
  return builder.finish();
}

export const indentGuides = ViewPlugin.fromClass(class {
  decorations: DecorationSet;
  constructor(view: EditorView) { this.decorations = build(view); }
  update(update: ViewUpdate) { if (update.docChanged || update.viewportChanged) this.decorations = build(update.view); }
}, { decorations: (plugin) => plugin.decorations });
