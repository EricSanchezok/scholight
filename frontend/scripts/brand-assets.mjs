import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import sharp from "sharp";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(scriptDirectory, "..");
const masterPath = path.join(webRoot, "brand/source/scholight-lynx-master.png");
const expectedMasterHash = "996d7777c6aa35932227196cb56b9ca32c0cc3b2be5a6c321d3b6001f3fc0579";

const mode = process.argv[2];
if (mode !== "build" && mode !== "check") {
  throw new Error("Usage: node scripts/brand-assets.mjs <build|check>");
}

const master = await readFile(masterPath);
const masterHash = createHash("sha256").update(master).digest("hex");
if (masterHash !== expectedMasterHash) {
  throw new Error(
    `Unexpected Scholight lynx master hash: ${masterHash}. Review and record intentional source changes.`,
  );
}

const masterMetadata = await sharp(master).metadata();
if (masterMetadata.width !== 720 || masterMetadata.height !== 720) {
  throw new Error(
    `Scholight lynx master must remain 720 × 720; received ${masterMetadata.width} × ${masterMetadata.height}.`,
  );
}

if (masterMetadata.channels !== 4) {
  throw new Error(
    `Scholight lynx master must be an RGBA PNG with transparent outer corners; received ${masterMetadata.channels} channels.`,
  );
}

const cornerCoordinates = [
  [0, 0],
  [719, 0],
  [0, 719],
  [719, 719],
];
const masterCorners = await Promise.all(
  cornerCoordinates.map(([left, top]) =>
    sharp(master).ensureAlpha().extract({ left, top, width: 1, height: 1 }).raw().toBuffer(),
  ),
);
if (masterCorners.some((corner) => corner[3] !== 0)) {
  throw new Error("Scholight lynx master must keep its outer corners transparent.");
}

const pngOptions = {
  compressionLevel: 9,
  effort: 10,
  palette: true,
  colours: 64,
  dither: 1,
};

function portrait(size) {
  return sharp(master)
    .resize(size, size, { fit: "cover", position: "centre" })
    .toColourspace("srgb")
    .png(pngOptions)
    .toBuffer();
}

function faviconFrame(size) {
  return sharp(master)
    .resize(size, size, { fit: "cover", position: "centre" })
    .toColourspace("srgb")
    .ensureAlpha()
    .png(pngOptions)
    .toBuffer();
}

function createIco(entries) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(entries.length, 4);

  const directory = Buffer.alloc(entries.length * 16);
  let offset = header.length + directory.length;
  entries.forEach(({ image, size }, index) => {
    const entryOffset = index * 16;
    directory.writeUInt8(size === 256 ? 0 : size, entryOffset);
    directory.writeUInt8(size === 256 ? 0 : size, entryOffset + 1);
    directory.writeUInt8(0, entryOffset + 2);
    directory.writeUInt8(0, entryOffset + 3);
    directory.writeUInt16LE(1, entryOffset + 4);
    directory.writeUInt16LE(32, entryOffset + 6);
    directory.writeUInt32LE(image.length, entryOffset + 8);
    directory.writeUInt32LE(offset, entryOffset + 12);
    offset += image.length;
  });

  return Buffer.concat([header, directory, ...entries.map(({ image }) => image)]);
}

async function opaquePortrait(size, artworkSize = size) {
  const artwork = await portrait(artworkSize);
  const inset = Math.round((size - artworkSize) / 2);
  return sharp({
    create: {
      width: size,
      height: size,
      channels: 4,
      background: "#FBFAF5",
    },
  })
    .composite([{ input: artwork, left: inset, top: inset }])
    .png(pngOptions)
    .toBuffer();
}

async function socialCard() {
  const artwork = await sharp(master)
    .resize(630, 630, { fit: "contain", position: "centre" })
    .toColourspace("srgb")
    .png()
    .toBuffer();
  return sharp({
    create: {
      width: 1200,
      height: 630,
      channels: 4,
      background: "#FBFAF5",
    },
  })
    .composite([{ input: artwork, left: 285, top: 0 }])
    .png(pngOptions)
    .toBuffer();
}

const faviconEntries = await Promise.all(
  [16, 32, 48].map(async (size) => ({
    image: await faviconFrame(size),
    size,
  })),
);

const portrait64 = await portrait(64);
const portrait128 = await portrait(128);
const touchIcon = await opaquePortrait(180);
const portrait192 = await portrait(192);
const portrait256 = await portrait(256);
const portrait512 = await portrait(512);
const portrait1024 = await portrait(1024);
const shareImage = await socialCard();

const assets = new Map([
  ["public/favicon.ico", createIco(faviconEntries)],
  ["public/apple-touch-icon.png", touchIcon],
  ["public/brand/scholight-lynx-portrait-64.png", portrait64],
  ["public/brand/scholight-lynx-portrait-128.png", portrait128],
  ["public/brand/icons/icon-192.png", portrait192],
  ["public/brand/icons/icon-512.png", portrait512],
  ["public/brand/icons/icon-maskable-512.png", await opaquePortrait(512, 410)],
  ["public/brand/social/og-image.png", shareImage],
  ["brand/exports/native/scholight-lynx-64.png", portrait64],
  ["brand/exports/native/scholight-lynx-128.png", portrait128],
  ["brand/exports/native/scholight-lynx-256.png", portrait256],
  ["brand/exports/native/scholight-lynx-512.png", portrait512],
  ["brand/exports/native/scholight-lynx-1024.png", portrait1024],
]);

const rasterDimensions = new Map([
  ["public/apple-touch-icon.png", [180, 180]],
  ["public/brand/scholight-lynx-portrait-64.png", [64, 64]],
  ["public/brand/scholight-lynx-portrait-128.png", [128, 128]],
  ["public/brand/icons/icon-192.png", [192, 192]],
  ["public/brand/icons/icon-512.png", [512, 512]],
  ["public/brand/icons/icon-maskable-512.png", [512, 512]],
  ["public/brand/social/og-image.png", [1200, 630]],
  ["brand/exports/native/scholight-lynx-64.png", [64, 64]],
  ["brand/exports/native/scholight-lynx-128.png", [128, 128]],
  ["brand/exports/native/scholight-lynx-256.png", [256, 256]],
  ["brand/exports/native/scholight-lynx-512.png", [512, 512]],
  ["brand/exports/native/scholight-lynx-1024.png", [1024, 1024]],
]);

for (const [relativePath, [expectedWidth, expectedHeight]] of rasterDimensions) {
  const generated = assets.get(relativePath);
  if (!generated) throw new Error(`Missing generated asset: ${relativePath}`);
  const metadata = await sharp(generated).metadata();
  if (metadata.width !== expectedWidth || metadata.height !== expectedHeight) {
    throw new Error(
      `${relativePath} must be ${expectedWidth} × ${expectedHeight}; received ${metadata.width} × ${metadata.height}.`,
    );
  }
}

for (const relativePath of [
  "public/apple-touch-icon.png",
  "public/brand/icons/icon-maskable-512.png",
  "public/brand/social/og-image.png",
]) {
  const stats = await sharp(assets.get(relativePath)).stats();
  const alpha = stats.channels[3];
  if (alpha && alpha.min !== 255) {
    throw new Error(`${relativePath} must be fully opaque.`);
  }
}

const failures = [];
for (const [relativePath, generated] of assets) {
  const destination = path.join(webRoot, relativePath);
  if (mode === "build") {
    await mkdir(path.dirname(destination), { recursive: true });
    await writeFile(destination, generated);
    continue;
  }

  try {
    const committed = await readFile(destination);
    if (!committed.equals(generated)) failures.push(relativePath);
  } catch {
    failures.push(relativePath);
  }
}

if (failures.length > 0) {
  throw new Error(
    `Generated brand assets are stale or missing:\n${failures
      .map((file) => `- ${file}`)
      .join("\n")}\nRun npm run brand:build and commit the results.`,
  );
}

process.stdout.write(
  mode === "build"
    ? `Generated ${assets.size} Scholight brand assets.\n`
    : `Verified ${assets.size} Scholight brand assets.\n`,
);
