// Reads uploaded files in the browser, so only text is sent to the server (Vercel rejects big uploads, and the files never need storing).
// pdf.js and JSZip are loaded from a CDN in index.html.

const PDF_WORKER = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
const PART_CHARS = 900_000; // the server accepts up to 1.2M characters per request

export async function extractFile(file) {
  const ext = file.name.split(".").pop().toLowerCase();
  let text;
  if (ext === "pdf") text = await extractPdf(file);
  else if (ext === "pptx") text = await extractPptx(file);
  else if (["txt", "md", "csv"].includes(ext)) text = await file.text();
  else throw new Error(`${file.name}: please use a PDF, PPTX, TXT or MD file.`);
  const body = text.replace(/\[(Page|Slide) \d+\]/g, "").trim();
  if (body.length < 20) {
    throw new Error(`${file.name}: I couldn't find any text in it. If it's a scanned image, try a text-based PDF, or paste the text into a .txt file.`);
  }
  return text;
}

async function extractPdf(file) {
  const pdfjs = window.pdfjsLib;
  if (!pdfjs) throw new Error("The PDF reader didn't load. Check your connection and reload the page.");
  pdfjs.GlobalWorkerOptions.workerSrc = PDF_WORKER;
  const doc = await pdfjs.getDocument({ data: await file.arrayBuffer() }).promise;
  const pages = [];
  for (let i = 1; i <= doc.numPages; i++) {
    const content = await (await doc.getPage(i)).getTextContent();
    let line = "", lastY = null;
    const lines = [];
    for (const it of content.items) {
      const y = it.transform ? it.transform[5] : null;
      if (lastY !== null && y !== null && Math.abs(y - lastY) > 2) { lines.push(line.trim()); line = ""; }
      line += it.str + (it.hasEOL ? "\n" : " ");
      if (y !== null) lastY = y;
    }
    lines.push(line.trim());
    pages.push(`[Page ${i}]\n` + lines.filter(Boolean).join("\n"));
  }
  return pages.join("\n");
}

async function extractPptx(file) {
  const JSZip = window.JSZip;
  if (!JSZip) throw new Error("The PowerPoint reader didn't load. Check your connection and reload the page.");
  const zip = await JSZip.loadAsync(await file.arrayBuffer());
  const num = (n) => parseInt(n.match(/(\d+)\.xml$/)[1], 10);
  const slides = Object.keys(zip.files).filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n)).sort((a, b) => num(a) - num(b));
  const paragraphs = (xml) => {
    const doc = new DOMParser().parseFromString(xml, "application/xml");
    return [...doc.getElementsByTagNameNS("*", "p")]
      .map((p) => [...p.getElementsByTagNameNS("*", "t")].map((t) => t.textContent).join(""))
      .filter((t) => t.trim());
  };
  const out = [];
  for (let i = 0; i < slides.length; i++) {
    const lines = paragraphs(await zip.file(slides[i]).async("string"));
    const notes = zip.file(`ppt/notesSlides/notesSlide${num(slides[i])}.xml`);
    if (notes) lines.push(...paragraphs(await notes.async("string")));
    out.push(`[Slide ${i + 1}]\n` + lines.join("\n"));
  }
  return out.join("\n");
}

// Very long files are sent in parts, cut at page or slide boundaries.
export function splitText(name, text) {
  if (text.length <= PART_CHARS) return [{ name, text }];
  const chunks = text.split(/\n(?=\[(?:Page|Slide) \d+\])/);
  const parts = [];
  let cur = "";
  for (const c of chunks) {
    if (cur && cur.length + c.length > PART_CHARS) { parts.push(cur); cur = ""; }
    cur += (cur ? "\n" : "") + c;
  }
  if (cur) parts.push(cur);
  return parts.map((t, i) => ({ name: `${name} (part ${i + 1})`, text: t.slice(0, PART_CHARS * 1.3) }));
}
