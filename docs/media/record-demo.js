// Record the README demo GIF: drive the editor through a full flow on the
// neutral "search-home" pack — drag a couple of elements into place, rotate one,
// flip it horizontally and vertically, then open the export JSON.
//
// This is how docs/media/demo.gif was produced. It's dev-only tooling, not part
// of the tool itself (EyeToSpec has zero runtime dependencies).
//
// Prerequisites:
//   1. A running server:   python3 serve.py --no-open
//   2. Playwright + ffmpeg installed:
//        npm i -g playwright && npx playwright install chromium
//        brew install ffmpeg        # or your platform's package manager
//
// Run:
//   node docs/media/record-demo.js
//   # then turn the .webm into a GIF:
//   cd /tmp/eyetospec-demo && WEBM=$(ls *.webm)
//   ffmpeg -y -i "$WEBM" -vf "fps=15,scale=800:-1:flags=lanczos,palettegen" /tmp/pal.png
//   ffmpeg -y -i "$WEBM" -i /tmp/pal.png \
//     -lavfi "fps=15,scale=800:-1:flags=lanczos[x];[x][1:v]paletteuse" \
//     docs/media/demo.gif

const { chromium } = require('playwright');

const URL = process.env.EYETOSPEC_URL || 'http://localhost:8770/editor.html?pack=search-home';
const OUT = '/tmp/eyetospec-demo';

async function drag(page, from, to, steps = 28) {
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  for (let i = 1; i <= steps; i++) {
    await page.mouse.move(
      from.x + (to.x - from.x) * (i / steps),
      from.y + (to.y - from.y) * (i / steps),
    );
    await page.waitForTimeout(12);
  }
  await page.mouse.up();
}

const center = (b) => ({ x: b.x + b.width / 2, y: b.y + b.height / 2 });

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    recordVideo: { dir: OUT, size: { width: 1280, height: 800 } },
  });
  const page = await context.newPage();
  await page.goto(URL, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);

  // Nudge the logo into a centered spot.
  const logo = page.locator('.el[data-id="logo"]');
  await logo.click();
  await page.waitForTimeout(600);
  let b = await logo.boundingBox();
  await drag(page, center(b), { x: center(b).x + 90, y: center(b).y - 20 });
  await page.waitForTimeout(500);

  // Slide the search bar under the logo.
  const bar = page.locator('.el[data-id="searchbar"]');
  await bar.click();
  await page.waitForTimeout(500);
  b = await bar.boundingBox();
  await drag(page, center(b), { x: center(b).x - 70, y: center(b).y + 20 });
  await page.waitForTimeout(600);

  // Grab the button, rotate it a touch, then show the flip toggles.
  const btn = page.locator('.el[data-id="btn_search"]');
  await btn.click();
  await page.waitForTimeout(500);
  const rot = btn.locator('.rot-handle');
  const rbox = await rot.boundingBox();
  const gbox = await btn.boundingBox();
  const pivot = center(gbox);
  await page.mouse.move(rbox.x + rbox.width / 2, rbox.y + rbox.height / 2);
  await page.mouse.down();
  for (let a = 0; a <= 60; a += 4) {
    const rad = (a - 90) * Math.PI / 180;
    await page.mouse.move(pivot.x + 100 * Math.cos(rad), pivot.y + 100 * Math.sin(rad));
    await page.waitForTimeout(22);
  }
  await page.mouse.up();
  await page.waitForTimeout(700);

  await page.locator('.flip-btn[data-flip="flipH"]').click();
  await page.waitForTimeout(900);
  await page.locator('.flip-btn[data-flip="flipV"]').click();
  await page.waitForTimeout(900);

  // Open the export JSON modal to show the payload.
  await page.locator('#export-btn').click();
  await page.waitForTimeout(1600);

  await context.close();
  await browser.close();
  console.log('recorded ->', OUT);
})();
