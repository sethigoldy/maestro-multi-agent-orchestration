// Splits unified diff text into lines tagged with their kind, for colouring.
export function parseDiff(text) {
  const lines = String(text || "").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  let inHeader = false;
  return lines.map((line) => {
    if (line.startsWith("diff --git ")) {
      inHeader = true;
      return { kind: "file", text: line };
    }
    if (line.startsWith("@@")) {
      inHeader = false;
      return { kind: "hunk", text: line };
    }
    if (inHeader) return { kind: "meta", text: line };
    if (line.startsWith("+")) return { kind: "add", text: line };
    if (line.startsWith("-")) return { kind: "del", text: line };
    return { kind: "context", text: line };
  });
}
