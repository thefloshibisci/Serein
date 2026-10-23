import { useLayoutEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { darkroomInkMarkup, playDarkroomInk } from "./darkroomInk.js";

function markdownSource(content) {
  if (Array.isArray(content)) return content.join("\n\n");
  return typeof content === "string" ? content : "";
}

export function MarkdownProjection({ content, className = "" }) {
  const markdown = markdownSource(content);
  const projectionRef = useRef(null);

  useLayoutEffect(() => {
    if (className.split(" ").includes("darkroom-letter-ink")) return playDarkroomInk(projectionRef.current);
    if (!className.split(" ").includes("darkroom-ink-reveal")) return undefined;
    for (const [index, block] of [...projectionRef.current.children].entries()) {
      block.style.setProperty("--ink-delay", `${index * 500}ms`);
    }
    return undefined;
  }, [markdown, className]);

  return (
    <div ref={projectionRef} className={`markdown-projection${className ? ` ${className}` : ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={className.split(" ").includes("darkroom-letter-ink") ? [darkroomInkMarkup] : []}
        skipHtml
        components={{
          a: ({ node: _node, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer" />
          ),
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
