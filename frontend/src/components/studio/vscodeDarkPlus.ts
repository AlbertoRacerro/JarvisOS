import { RangeSetBuilder } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, type DecorationSet, type ViewUpdate } from "@codemirror/view";
import { HighlightStyle, syntaxHighlighting, syntaxTree } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";
import type { SyntaxNode } from "@lezer/common";

// Workshop Python pane: VS Code "Dark+" token colours. The code stays dark in both app themes; the editor chrome
// (background, gutter, active line, cursor) is in studio.css.
export const darkPlus = {
  foreground: "#d4d4d4", controlKeyword: "#c586c0", keyword: "#569cd6", function: "#dcdcaa", string: "#ce9178",
  number: "#b5cea8", comment: "#6a9955", variable: "#9cdcfe", type: "#4ec9b0", escape: "#d7ba7d", invalid: "#f44747"
};

const pythonHighlight = HighlightStyle.define([
  { tag: [t.controlKeyword, t.moduleKeyword], color: darkPlus.controlKeyword },
  { tag: [t.keyword, t.definitionKeyword, t.operatorKeyword, t.modifier, t.bool, t.null, t.atom, t.self], color: darkPlus.keyword },
  { tag: [t.function(t.variableName), t.function(t.definition(t.variableName)), t.function(t.propertyName), t.meta], color: darkPlus.function },
  { tag: [t.string, t.special(t.string)], color: darkPlus.string },
  { tag: t.escape, color: darkPlus.escape },
  { tag: t.number, color: darkPlus.number },
  { tag: t.comment, color: darkPlus.comment },
  { tag: [t.variableName, t.propertyName, t.labelName], color: darkPlus.variable },
  { tag: [t.className, t.definition(t.className), t.typeName, t.namespace], color: darkPlus.type },
  { tag: [t.operator, t.punctuation, t.paren, t.squareBracket, t.brace, t.separator], color: darkPlus.foreground },
  { tag: t.invalid, color: darkPlus.invalid }
]);

// Lezer tags cannot tell UPPER_CASE constants or annotation types apart from ordinary names; mark them from the tree.
const constantMark = Decoration.mark({ class: "cm-py-constant" });
const typeMark = Decoration.mark({ class: "cm-py-type" });
const inAnnotation = (node: SyntaxNode) => { for (let parent = node.parent, depth = 0; parent && depth < 4; parent = parent.parent, depth++) if (parent.name === "TypeDef") return true; return false; };
function semanticMarks(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  const tree = syntaxTree(view.state);
  for (const { from, to } of view.visibleRanges) tree.iterate({ from, to, enter: (ref) => {
    if (ref.name !== "VariableName") return;
    const name = view.state.doc.sliceString(ref.from, ref.to);
    if (name.length > 1 && /^[A-Z][A-Z0-9_]*$/.test(name)) builder.add(ref.from, ref.to, constantMark);
    else if (inAnnotation(ref.node)) builder.add(ref.from, ref.to, typeMark);
  } });
  return builder.finish();
}
const semanticHighlight = ViewPlugin.fromClass(class {
  decorations: DecorationSet;
  constructor(view: EditorView) { this.decorations = semanticMarks(view); }
  update(update: ViewUpdate) { if (update.docChanged || update.viewportChanged || syntaxTree(update.startState) !== syntaxTree(update.state)) this.decorations = semanticMarks(update.view); }
}, { decorations: (plugin) => plugin.decorations });

// `dark: true` switches CodeMirror's own selection/cursor defaults to their dark variants.
const darkBase = EditorView.theme({
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection": { backgroundColor: "#264f78" }
}, { dark: true });

export const vscodeDarkPlus = [darkBase, syntaxHighlighting(pythonHighlight), semanticHighlight];
