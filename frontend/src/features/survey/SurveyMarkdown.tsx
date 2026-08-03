import ReactMarkdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { styles } from "../../styles/classes";
import { resolveReportImage } from "./survey";

const HTML_COMMENT_PATTERN = /<!--[\s\S]*?-->/g;

export function SurveyMarkdown({
  markdown,
  imageArtifacts,
  compact = false,
  preview = false,
}: {
  markdown: string;
  imageArtifacts?: Map<string, string>;
  compact?: boolean;
  preview?: boolean;
}) {
  const components: Components = {
    a: ({ children, href }) =>
      preview ? (
        <span>{children}</span>
      ) : (
        <a href={href} target="_blank" rel="noreferrer noopener">
          {children}
        </a>
      ),
    img: ({ alt, src }) => {
      if (!src || !imageArtifacts) return null;
      const resolved = resolveReportImage(src, imageArtifacts);
      return resolved ? <img src={resolved} alt={alt ?? ""} loading="lazy" /> : null;
    },
  };

  return (
    <div className={`${styles.surveyMarkdown} ${compact ? styles.surveyMarkdownCompact : ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={components}
        urlTransform={(url, key, node) => {
          if (key === "src" && node.tagName === "img") return url;
          return defaultUrlTransform(url);
        }}
      >
        {markdown.replace(HTML_COMMENT_PATTERN, "")}
      </ReactMarkdown>
    </div>
  );
}
