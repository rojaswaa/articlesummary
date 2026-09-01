// PDF Viewer functionality
let pdfDoc = null;
let currentPage = 1;
let pdfFolder = null;
let isPdfVisible = false;

const pdfPanel = document.getElementById('pdf-panel');
const togglePdfBtn = document.getElementById('toggle-pdf-btn');
const closePdfBtn = document.getElementById('close-pdf-btn');
const pdfCanvasContainer = document.getElementById('pdf-canvas-container');
const pdfPageInput = document.getElementById('pdf-page-input');
const pdfTotalPages = document.getElementById('pdf-total-pages');
const pdfPrevBtn = document.getElementById('pdf-prev-btn');
const pdfNextBtn = document.getElementById('pdf-next-btn');

// Toggle PDF panel visibility
if (togglePdfBtn) {
  togglePdfBtn.addEventListener('click', () => {
    if (isPdfVisible) {
      hidePdfPanel();
    } else {
      showPdfPanel();
    }
  });
}

if (closePdfBtn) {
  closePdfBtn.addEventListener('click', hidePdfPanel);
}

// Navigation controls
if (pdfPrevBtn) {
  pdfPrevBtn.addEventListener('click', () => {
    if (currentPage > 1) {
      currentPage--;
      pdfPageInput.value = currentPage;
      renderPdfPage();
    }
  });
}

if (pdfNextBtn) {
  pdfNextBtn.addEventListener('click', () => {
    if (pdfDoc && currentPage < pdfDoc.numPages) {
      currentPage++;
      pdfPageInput.value = currentPage;
      renderPdfPage();
    }
  });
}

if (pdfPageInput) {
  pdfPageInput.addEventListener('change', () => {
    const page = Math.max(1, Math.min(parseInt(pdfPageInput.value) || 1, pdfDoc?.numPages || 1));
    currentPage = page;
    pdfPageInput.value = page;
    renderPdfPage();
  });
}

function showPdfPanel() {
  isPdfVisible = true;
  if (pdfPanel) pdfPanel.classList.add('visible');
  if (togglePdfBtn) togglePdfBtn.style.opacity = '0.5';
}

function hidePdfPanel() {
  isPdfVisible = false;
  if (pdfPanel) pdfPanel.classList.remove('visible');
  if (togglePdfBtn) togglePdfBtn.style.opacity = '1';
}

async function loadPdf(folder, relativePath) {
  try {
    pdfFolder = folder;
    // Construct URL to load PDF from Flask backend
    const fullPath = `${folder}/${relativePath}`.replace(/\\/g, '/');
    const encodedPath = encodeURIComponent(fullPath);
    const pdfUrl = `/pdf?path=${encodedPath}`;

    pdfDoc = await pdfjsLib.getDocument(pdfUrl).promise;
    currentPage = 1;
    pdfPageInput.value = 1;
    pdfTotalPages.textContent = pdfDoc.numPages;

    if (pdfDoc) {
      showPdfPanel();
      renderPdfPage();
    }
  } catch (error) {
    console.error('Error loading PDF:', error);
    pdfCanvasContainer.innerHTML = `<p style="color: #f87171; padding: 16px;">Error loading PDF: ${error.message}</p>`;
  }
}

async function renderPdfPage() {
  if (!pdfDoc) return;

  try {
    const page = await pdfDoc.getPage(currentPage);
    const scale = 1.5;
    const viewport = page.getViewport({ scale });

    // Create canvas
    const canvas = document.createElement('canvas');
    const context = canvas.getContext('2d');
    canvas.width = viewport.width;
    canvas.height = viewport.height;

    // Render page to canvas
    const renderContext = {
      canvasContext: context,
      viewport: viewport,
    };
    await page.render(renderContext).promise;

    // Replace canvas in container
    pdfCanvasContainer.innerHTML = '';
    pdfCanvasContainer.appendChild(canvas);
  } catch (error) {
    console.error('Error rendering PDF page:', error);
  }
}

// Add OCR badge color for macocr
const style = document.createElement('style');
style.textContent = `.ocr-badge.macocr { background: rgba(59,130,246,0.15); color: #93c5fd; border: 1px solid rgba(59,130,246,0.3); }`;
document.head.appendChild(style);
