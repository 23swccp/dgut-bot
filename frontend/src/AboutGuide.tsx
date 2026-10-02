import { isValidElement, type ReactNode, useEffect, useMemo, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import guideSource from "../../README.md?raw";

type AboutGuideProps = {
  version: string;
  repo: string;
};

type TocItem = {
  id: string;
  label: string;
};

type TocGroups = {
  tutorial: TocItem[];
  developer: TocItem[];
};

const FIRST_DEVELOPER_HEADING = "开发环境与本地运行";
const GITHUB_HEADER_PATTERN = /<!-- github-readme-header:start -->[\s\S]*?<!-- github-readme-header:end -->/;
const APP_MARKDOWN_HEADER = `![莞工小皮卡](./docs/dgut-bot-hero.png)

# dgut-bot

莞工小皮卡

[![Python 3.10+](./docs/badges/python.svg)](https://www.python.org/) [![Windows](./docs/badges/windows.svg)](https://www.microsoft.com/windows/) [![v{{VERSION}}](./docs/badges/version.svg)]({{REPO_URL}}/releases) [![GitHub dgut-bot](./docs/badges/github.svg)]({{REPO_URL}})`;

export function markdownForApp(source: string): string {
  return source.replace(GITHUB_HEADER_PATTERN, APP_MARKDOWN_HEADER);
}

function plainMarkdown(text: string): string {
  return text
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .replace(/[`*_~]/g, "")
    .trim();
}

function headingId(label: string): string {
  return plainMarkdown(label)
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[^\p{Letter}\p{Number}\s-]/gu, "")
    .trim()
    .replace(/\s+/g, "-");
}

function parseToc(source: string): TocGroups {
  const groups: TocGroups = { tutorial: [], developer: [] };
  let group: keyof TocGroups = "tutorial";
  source.split(/\r?\n/).forEach(line => {
    const match = /^##\s+(.+?)\s*$/.exec(line);
    if (!match) return;
    const label = plainMarkdown(match[1]);
    if (label === FIRST_DEVELOPER_HEADING) {
      group = "developer";
    }
    groups[group].push({ id: headingId(label), label });
  });
  return groups;
}

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement(node)) return nodeText((node.props as { children?: ReactNode }).children);
  return "";
}

function containsImage(node: ReactNode): boolean {
  if (Array.isArray(node)) return node.some(containsImage);
  if (!isValidElement(node)) return false;
  if (node.type === "img") return true;
  const props = node.props as { children?: ReactNode; src?: unknown };
  if (typeof props.src === "string") return true;
  return containsImage(props.children);
}

function LinkedHeading({ level, children }: { level: 2 | 3; children: ReactNode }) {
  const label = nodeText(children);
  const id = headingId(label);
  const content = <>{children}<a className="header-anchor" href={`#${id}`} aria-label={`${label}的永久链接`}>#</a></>;
  return level === 2 ? <h2 id={id} data-toc>{content}</h2> : <h3 id={id} data-toc>{content}</h3>;
}

const markdownComponents: Components = {
  h2: ({ children }) => <LinkedHeading level={2}>{children}</LinkedHeading>,
  h3: ({ children }) => <LinkedHeading level={3}>{children}</LinkedHeading>,
  a: ({ href = "", children }) => {
    const external = /^https?:\/\//i.test(href);
    const showExternalMark = external && !containsImage(children);
    return (
      <a href={href} target={external ? "_blank" : undefined} rel={external ? "noreferrer noopener" : undefined}>
        {children}
        {showExternalMark && <span className="external-link-mark" aria-hidden="true">↗</span>}
      </a>
    );
  },
  img: ({ src = "", alt = "" }) => {
    const decorative = src.includes("/docs/badges/") || src.endsWith("/docs/dgut-bot-hero.png");
    if (decorative || !alt) return <img src={src} alt={alt} loading="lazy" />;
    return (
      <span className="markdown-figure">
        <img src={src} alt={alt} loading="lazy" />
        <span className="markdown-figure-caption">{alt}</span>
      </span>
    );
  },
};

export function AboutGuide({ version, repo }: AboutGuideProps) {
  const repoUrl = repo ? `https://github.com/${repo}` : "https://github.com/23swccp/dgut-bot";
  const source = useMemo(() => markdownForApp(guideSource)
    .replace(/\{\{VERSION\}\}/g, version || "…")
    .replace(/\{\{REPO_URL\}\}/g, repoUrl), [repoUrl, version]);
  const tocGroups = useMemo(() => parseToc(source), [source]);
  const tocItems = useMemo(() => [...tocGroups.tutorial, ...tocGroups.developer], [tocGroups]);
  const [activeSection, setActiveSection] = useState(tocItems[0]?.id || "");

  useEffect(() => {
    const scrollRoot = document.querySelector<HTMLElement>(".about-page")?.closest<HTMLElement>(".settings-body") || window;
    const updateActiveSection = () => {
      let current = tocItems[0]?.id || "";
      for (const item of tocItems) {
        const target = document.getElementById(item.id);
        if (target && target.getBoundingClientRect().top <= 150) current = item.id;
        else break;
      }
      setActiveSection(current);
    };

    updateActiveSection();
    scrollRoot.addEventListener("scroll", updateActiveSection, { passive: true });
    return () => scrollRoot.removeEventListener("scroll", updateActiveSection);
  }, [tocItems]);

  return (
    <section className="about-page" aria-label="使用教程与关于">
      <article className="about-document">
        <main className="about-content">
          <div className="about-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{source}</ReactMarkdown>
          </div>
        </main>

        <aside className="about-sidebar">
          <nav className="about-toc" aria-label="教程与开发者文档">
            <strong>教程</strong>
            {tocGroups.tutorial.map(item => (
              <a
                key={item.id}
                className={activeSection === item.id ? "active" : undefined}
                href={`#${item.id}`}
                onClick={() => setActiveSection(item.id)}
              >
                {item.label}
              </a>
            ))}
            <strong className="toc-group-title">开发者文档</strong>
            {tocGroups.developer.map(item => (
              <a
                key={item.id}
                className={activeSection === item.id ? "active" : undefined}
                href={`#${item.id}`}
                onClick={() => setActiveSection(item.id)}
              >
                {item.label}
              </a>
            ))}
          </nav>
        </aside>
      </article>
    </section>
  );
}
