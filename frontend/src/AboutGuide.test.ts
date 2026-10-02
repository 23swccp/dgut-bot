import { describe, expect, it } from "vitest";

import { markdownForApp } from "./AboutGuide";

describe("markdownForApp", () => {
  it("restores the app header while GitHub keeps its centered HTML header", () => {
    const githubHeader = `before
<!-- github-readme-header:start -->
<p align="center"><img src="./docs/dgut-bot-hero.png" width="340"></p>
<p align="center">badges</p>
<h1 align="center">dgut-bot</h1>
<!-- github-readme-header:end -->
after`;

    const result = markdownForApp(githubHeader);

    expect(result).toContain("before\n![莞工小皮卡](./docs/dgut-bot-hero.png)");
    expect(result).toContain("# dgut-bot\n\n莞工小皮卡\n\n[![Python 3.10+]");
    expect(result).toContain("after");
    expect(result).not.toContain("github-readme-header");
    expect(result).not.toContain("<p align=");
  });
});
