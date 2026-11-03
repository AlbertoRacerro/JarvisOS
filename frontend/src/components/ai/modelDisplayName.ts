// Spec 161: concise human model names for ordinary chat metadata. The exact
// recorded model/provider ids stay in the info disclosure; this only presents
// them. Unknown ids are humanised from their own tokens, never invented.

const FAMILIES: Record<string, string> = {
  claude: "Claude", gemma: "Gemma", qwen: "Qwen", gpt: "GPT", llama: "Llama", mistral: "Mistral",
  ministral: "Ministral", deepseek: "DeepSeek", phi: "Phi", lfm: "LFM", granite: "Granite",
  opus: "Opus", sonnet: "Sonnet", haiku: "Haiku", fable: "Fable", luna: "Luna", sol: "Sol",
  astra: "Astra", terra: "Terra", flash: "Flash", pro: "Pro", mini: "Mini", nano: "Nano"
};
// Packaging and quantisation noise that never belongs in a human name.
const NOISE = /^(it|instruct|chat|qat|gguf|bf16|fp16|f16|f32|awq|gptq|exl2|imatrix|ud|latest|preview|q\d.*|iq\d.*|k|m|s|xl|xs|l)$/i;

export function modelDisplayName(modelId: string | null | undefined, agent?: string | null): string {
  if (!modelId) return agent ? FAMILIES[agent] ?? capitalise(agent) : "Unknown model";
  const base = modelId.split(/[\\/]/).pop()!.replace(/\.gguf$/i, "").replace(/@.*$/, "");
  // Quantisation tags such as q4_0 or q5_k_m are dropped whole before "_" separates words.
  const tokens = base.toLowerCase().split(/[-\s]+/).flatMap((token) => (NOISE.test(token) ? [] : token.split("_"))).filter(Boolean);
  const words: string[] = [];
  for (let index = 0; index < tokens.length; index += 1) {
    let token = tokens[index];
    if (NOISE.test(token) || /^\d{4,8}$/.test(token)) continue;
    const glued = /^([a-z]+)(\d+(?:\.\d+)?)$/.exec(token);
    if (glued && FAMILIES[glued[1]]) { words.push(FAMILIES[glued[1]], glued[2]); continue; }
    if (/^\d+(\.\d+)?[bm]$/.test(token)) { words.push(token.toUpperCase()); continue; }
    if (/^v\d+(\.\d+)?$/.test(token)) { words.push(token.toUpperCase()); continue; }
    if (/^\d+$/.test(token) && /^\d+$/.test(tokens[index + 1] ?? "") && tokens[index + 1].length <= 2) {
      token = `${token}.${tokens[index + 1]}`;
      index += 1;
    }
    words.push(FAMILIES[token] ?? (/^\d/.test(token) ? token : capitalise(token)));
  }
  if (!words.length) return base;
  // "GPT 6 Luna" reads as "GPT-6 Luna", matching the vendor's own naming.
  if (words[0] === "GPT" && /^\d/.test(words[1] ?? "")) words.splice(0, 2, `GPT-${words[1]}`);
  return words.join(" ");
}

function capitalise(word: string): string {
  return word ? word[0].toUpperCase() + word.slice(1) : word;
}
