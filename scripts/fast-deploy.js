#!/usr/bin/env node
/**
 * 快速部署（开发迭代用）：只编译 Flutter 发行版并替换 D:\SuperBrain 的 AOT 产物。
 *
 * 与「全量定稿」的区别：
 *   - 不跑 Windows 安装器打包（不生成 setup.exe / 不改动 exe、flutter_windows.dll）
 *   - 后端是 dev 态源码直跑，前端改动只需换 data/app.so + data/flutter_assets
 *   - 替换前自动备份旧 app.so，可随时回退
 *
 * 用法：node scripts/fast-deploy.js
 * 全部定稿后再单独跑安装器全量编译。
 */
'use strict';
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const APP = path.join(ROOT, 'flutter_app');
const OUT = path.join(APP, 'build', 'windows', 'x64', 'runner', 'Release', 'data');
const DEST = 'D:\\SuperBrain\\data';
const FLUTTER = 'D:\\flutter\\bin\\flutter.bat';

function stamp() {
  return new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
}

function main() {
  if (!fs.existsSync(FLUTTER)) throw new Error(`Flutter SDK 不存在：${FLUTTER}`);
  if (!fs.existsSync(DEST)) throw new Error(`安装目录不存在：${DEST}`);

  console.log('[1/3] 编译 Flutter Windows 发行版（不打包安装器）…');
  const t0 = Date.now();
  const build = spawnSync(FLUTTER, ['build', 'windows', '--release'], {
    cwd: APP,
    stdio: 'inherit',
    shell: true,
  });
  if (build.status !== 0) {
    throw new Error(`flutter build 失败，退出码 ${build.status}`);
  }
  console.log(`[1/3] 编译完成，用时 ${((Date.now() - t0) / 1000).toFixed(1)}s`);

  const soSrc = path.join(OUT, 'app.so');
  const assetsSrc = path.join(OUT, 'flutter_assets');
  if (!fs.existsSync(soSrc)) throw new Error(`未找到 AOT 产物：${soSrc}`);

  console.log('[2/3] 备份旧 app.so …');
  const soDst = path.join(DEST, 'app.so');
  const bak = fs.existsSync(soDst)
    ? `${soDst}.bak.${stamp()}`
    : null;
  if (bak) {
    fs.copyFileSync(soDst, bak);
    console.log(`      备份 → ${bak}`);
  }

  console.log('[3/3] 替换 app.so 与 flutter_assets …');
  fs.copyFileSync(soSrc, soDst);
  if (fs.existsSync(assetsSrc)) {
    fs.cpSync(assetsSrc, path.join(DEST, 'flutter_assets'), {
      recursive: true,
      force: true,
    });
  }
  const size = fs.statSync(soDst).size;
  console.log('完成：app.so =', size, '字节；exe / dll / 安装器均未改动。');
  console.log('下一步：重启 D:\\SuperBrain\\super_brain.exe 即可直接测试。');
}

try {
  main();
} catch (error) {
  console.error('快速部署失败：', error.message);
  process.exit(1);
}
