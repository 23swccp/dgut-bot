import { describe, expect, it } from "vitest";
import { defaultExportOptions, exportSelectionKey, selectionAlreadyExported, type ExportStatus } from "./coursewareView";

function completed(): ExportStatus {
  const result = { state: "completed", courseId: "1", courseName: "course", chapterTotal: 1, chapterDone: 1,
    pageTotal: 1, pageWritten: 1, pageUnchanged: 0, fileWritten: 1, failures: [], folder: "courseware/course", error: "" };
  return { ...result, root: "courseware", courseTotal: 1, courseDone: 1, results: [result],
    request: { courseIds: ["1"], options: defaultExportOptions() } };
}

describe("completed courseware selection", () => {
  it("recognizes a successful selection and permits a different course, type or chapter", () => {
    const status = completed(), options = defaultExportOptions();
    expect(selectionAlreadyExported(status, [1], options)).toBe(true);
    expect(selectionAlreadyExported(status, [2], options)).toBe(false);
    expect(selectionAlreadyExported(status, [1], { ...options, sources: ["courseware"] })).toBe(false);
    expect(selectionAlreadyExported(status, [1], { ...options, kinds: ["document"] })).toBe(false);
    expect(selectionAlreadyExported(status, [1], { ...options, chapters: { "1": ["c2"] } })).toBe(false);
    expect(selectionAlreadyExported({ ...status, folderOpenError: "open failed" }, [1], options)).toBe(true);
  });

  it("allows retry after cancellation, auth errors and partial failures", () => {
    const status = completed(), options = defaultExportOptions();
    for (const state of ["running", "cancelling", "cancelled", "error", "auth_required"]) {
      expect(selectionAlreadyExported({ ...status, state }, [1], options)).toBe(false);
    }
    expect(selectionAlreadyExported({ ...status, results: [{ ...status.results[0], failures: [{ reason: "failed" }] }] }, [1], options)).toBe(false);
    expect(selectionAlreadyExported({ ...status, request: null }, [1], options)).toBe(false);
    expect(selectionAlreadyExported(null, [1], options)).toBe(false);
  });

  it("ignores selection order and hidden source chapters", () => {
    const options = { sources: ["courseware"], kinds: ["text", "document"], chapters: { "1": ["c2", "c1", "homework:1:9"] } };
    const equivalent = { ...options, kinds: ["document", "text"], chapters: { "1": ["c1", "c2"] } };
    expect(exportSelectionKey([1, 2], options)).toBe(exportSelectionKey(["2", "1"], equivalent));
  });
});
