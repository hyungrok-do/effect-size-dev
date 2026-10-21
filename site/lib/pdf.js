// The text layer of a PDF, for checking quotes. pdf.js is loaded the first time it is needed.

let pdfjs = null;

export async function pdfText(bytes) {
  try {
    if (!pdfjs) {
      pdfjs = await import('../vendor/pdf.min.mjs');
      pdfjs.GlobalWorkerOptions.workerSrc = new URL('../vendor/pdf.worker.min.mjs', import.meta.url).href;
    }
    // pdf.js takes ownership of the buffer it is given, so hand it a copy.
    const task = pdfjs.getDocument({ data: bytes.slice() });
    try {
      const doc = await task.promise;
      const pages = [];
      for (let n = 1; n <= doc.numPages; n++) {
        const content = await (await doc.getPage(n)).getTextContent();
        pages.push(content.items.map((item) => item.str + (item.hasEOL ? '\n' : ' ')).join(''));
      }
      return pages.join('\n');
    } finally {
      await task.destroy();
    }
  } catch (error) {
    // Without a text layer the quotes can't be checked, so every value is flagged for review.
    console.warn('Could not read the text of the PDF:', error);
    return '';
  }
}
