import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { filterSignChoices, firstSignChoiceCursor, SelectedSignChoice, SignChoicePicker, stepSignChoiceCursor } from "./SignChoicePicker";

const choices = [
  { id: 11, title: "旧课堂", detail: "进行中 · 2026/10/14" },
  { id: 22, title: "新课堂", detail: "已结束 · 2026/10/21", disabled: true },
];

describe("shared sign picker", () => {
  it("searches title, status, date and ID without excluding older classrooms", () => {
    expect(filterSignChoices(choices, "")).toEqual(choices);
    expect(filterSignChoices(choices, "进行中")).toEqual([choices[0]]);
    expect(filterSignChoices(choices, " 22 ")).toEqual([choices[1]]);
    expect(filterSignChoices(choices, "10/14")).toEqual([choices[0]]);
  });
  it("renders the selected option and disables choices during a request", () => {
    const html = renderToStaticMarkup(<SignChoicePicker label="选择签到课堂" items={choices} total={2}
      cursor={0} busy={true} onCursor={() => {}} onChoose={() => {}} />);
    expect(html).toContain('aria-label="选择签到课堂"');
    expect(html).toContain('aria-selected="true"');
    expect(html.match(/disabled=""/g)).toHaveLength(2);
    expect(html).toContain("旧课堂");
    expect(html).toContain("进行中");
  });
  it("greys out an ended classroom and keeps it out of the keyboard cursor", () => {
    const html = renderToStaticMarkup(<SignChoicePicker label="选择签到课堂" items={choices} total={2}
      cursor={0} busy={false} onCursor={() => {}} onChoose={() => {}} />);
    expect(html.match(/disabled=""/g)).toHaveLength(1);
    expect(html).toContain('aria-disabled="true"');
    expect(html).toContain("is-disabled");
    expect(html).toContain("已结束的课堂不能作为监测目标");
    expect(stepSignChoiceCursor(choices, 0, 1)).toBe(0);
    expect(stepSignChoiceCursor(choices, 0, -1)).toBe(0);
    expect(firstSignChoiceCursor(choices)).toBe(0);
    expect(firstSignChoiceCursor([choices[1]])).toBe(0);
  });
  it("moves the cursor to the first selectable row when the list starts with an ended classroom", () => {
    const ended = [{ id: 33, title: "已结束", detail: "已结束 · 2026/10/20", disabled: true }, choices[0]];
    expect(firstSignChoiceCursor(ended)).toBe(1);
    expect(stepSignChoiceCursor(ended, 1, 1)).toBe(1);
    expect(stepSignChoiceCursor(ended, 1, -1)).toBe(1);
  });
  it("echoes the chosen classroom as the same row used by the list", () => {
    const card = renderToStaticMarkup(<SelectedSignChoice item={choices[0]} busy={false} onChange={() => {}} />);
    expect(card).toContain('aria-label="已选择的课堂"');
    expect(card).toContain("class=\"course-option active\"");
    expect(card).toContain("旧课堂");
    expect(card).toContain("进行中 · 2026/10/14");
    expect(card).toContain("ID 11");
    expect(card).toContain("更换课堂");
    expect(renderToStaticMarkup(<SelectedSignChoice item={choices[0]} busy={false} />)).not.toContain("更换课堂");
  });
});