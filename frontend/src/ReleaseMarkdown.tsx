import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkGithub from "remark-github";
import remarkBreaks from "remark-breaks";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";

const components: Components = {
  a: ({ node: _node, href, children, ...props }) => {
    if (!href) return <span>{children}</span>;
    const external = !href.startsWith("#");
    // sanitize 会给 id 加此前缀，页内链接必须指向相同的目标。
    return <a {...props} href={external ? href : `#user-content-${href.slice(1)}`} target={external ? "_blank" : undefined}
      rel={external ? "noopener noreferrer" : undefined}>{children}</a>;
  },
  img: ({ node: _node, ...props }) => <img {...props} loading="lazy" />,
  table: ({ children, ...props }) => {
    const { node: _node, ...attributes } = props;
    return <div className="release-markdown-table"><table {...attributes}>{children}</table></div>;
  },
};

export function ReleaseMarkdown({ source, repo }: { source: string; repo: string }) {
  return <div className="release-markdown">
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkBreaks, [remarkGithub, { repository: repo, mentionStrong: false }]]}
      remarkRehypeOptions={{ clobberPrefix: "" }}
      // 支持 Release 中的折叠内容、图片等 HTML，并采用 GitHub 风格的安全白名单。
      rehypePlugins={[rehypeRaw, rehypeSanitize]}
      components={components}
    >{source}</ReactMarkdown>
  </div>;
}
