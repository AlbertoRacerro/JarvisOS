import { useEffect, useRef } from "react";
import { EditorState, StateEffect, StateField } from "@codemirror/state";
import { Decoration, EditorView, keymap, lineNumbers } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { python } from "@codemirror/lang-python";
import { bracketMatching, indentOnInput, indentUnit } from "@codemirror/language";
import { indentGuides } from "./indentGuides";
import { vscodeDarkPlus } from "./vscodeDarkPlus";

// Spec 230: a read-only (published) source stays focusable and selectable, so the cursor, bracket matching, selection
// and copy work; EditorState.readOnly still refuses every edit.
// Spec 214/#920: persistent line decoration marking the Python line linked to a clicked equation.
const setLinkedLineEffect = StateEffect.define<number | null>();
const linkedLineMark = Decoration.line({ class: "cm-linked-line" });
const linkedLineField = StateField.define({
  create: () => Decoration.none,
  update(decorations, transaction) {
    let next = decorations.map(transaction.changes);
    for (const effect of transaction.effects) if (effect.is(setLinkedLineEffect)) next = effect.value && effect.value <= transaction.state.doc.lines ? Decoration.set([linkedLineMark.range(transaction.state.doc.line(effect.value).from)]) : Decoration.none;
    return next;
  },
  provide: (field) => EditorView.decorations.from(field)
});

export type LineRange = { from: number; to: number };
type Props = { value: string; onChange(value: string): void; disabled?: boolean; focusLine?: number | null; onCursorLineChange?(line: number): void; onSelectionLinesChange?(range: LineRange): void; linkedLine?: number | null };
export default function PythonEditor({ value, onChange, disabled = false, focusLine, onCursorLineChange, onSelectionLinesChange, linkedLine }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const callback = useRef(onChange);
  callback.current = onChange;
  const cursorCallback = useRef(onCursorLineChange);
  cursorCallback.current = onCursorLineChange;
  const rangeCallback = useRef(onSelectionLinesChange);
  rangeCallback.current = onSelectionLinesChange;
  const linked = useRef(linkedLine);
  linked.current = linkedLine;
  useEffect(() => {
    if (!host.current) return;
    const editor = new EditorView({
      parent: host.current,
      state: EditorState.create({ doc: value, extensions: [lineNumbers(), history(), keymap.of([...defaultKeymap, ...historyKeymap]), python(), indentUnit.of("    "), ...vscodeDarkPlus,bracketMatching(), indentOnInput(), indentGuides, linkedLineField, EditorView.lineWrapping, EditorState.readOnly.of(disabled), EditorView.updateListener.of((update) => {
        if (update.docChanged) callback.current(update.state.doc.toString());
        if (update.selectionSet || update.docChanged) cursorCallback.current?.(update.state.doc.lineAt(update.state.selection.main.head).number);
        if (update.selectionSet || update.docChanged) { const { main } = update.state.selection; rangeCallback.current?.({ from: update.state.doc.lineAt(main.from).number, to: update.state.doc.lineAt(main.to).number }); }
      })] })
    });
    if (linked.current) editor.dispatch({ effects: setLinkedLineEffect.of(linked.current) });
    view.current = editor;
    return () => { editor.destroy(); view.current = null; };
  }, [disabled]);
  useEffect(() => {
    const editor = view.current;
    if (editor && editor.state.doc.toString() !== value) editor.dispatch({ changes: { from: 0, to: editor.state.doc.length, insert: value } });
  }, [value]);
  useEffect(() => { view.current?.dispatch({ effects: setLinkedLineEffect.of(linkedLine ?? null) }); }, [linkedLine]);
  useEffect(() => {
    const editor = view.current;
    if (!editor || !focusLine || focusLine > editor.state.doc.lines) return;
    const position = editor.state.doc.line(focusLine).from;
    editor.dispatch({ selection: { anchor: position }, scrollIntoView: true });
    editor.focus();
  }, [focusLine]);
  return <div className="studio-code-editor" ref={host} aria-label="Python source editor" />;
}
