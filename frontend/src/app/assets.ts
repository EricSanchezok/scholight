export const brandAssets = Object.freeze({
  portrait64: "/brand/scholight-lynx-portrait-64.png",
  portrait128: "/brand/scholight-lynx-portrait-128.png",
  icon192: "/brand/icons/icon-192.png",
  icon512: "/brand/icons/icon-512.png",
});

export const brandMarkSources = [
  `${brandAssets.portrait64} 64w`,
  `${brandAssets.portrait128} 128w`,
  `${brandAssets.icon192} 192w`,
  `${brandAssets.icon512} 512w`,
].join(", ");
