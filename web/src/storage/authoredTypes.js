export async function authoredType(tool, args = {}, signal) {
  const response = await fetch("/__serein/authored-types", {
    method: "POST", signal, headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tool, arguments: args }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "没有保存，请刷新确认当前内容后重试。");
  return result;
}
