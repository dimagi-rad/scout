'use strict';

const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const { readFileSync, realpathSync, writeFileSync } = require('node:fs');
const { createRequire } = require('node:module');
const { dirname, join, resolve } = require('node:path');

// The pinned Cube version every build-time dependency patch was audited against.
const PINNED_VERSION = '1.6.39';

function sha256(content) {
  return createHash('sha256').update(content).digest('hex');
}

// Replaces one module of the serving Cube install with a Scout module, refusing
// unless the package version and the exact upstream file hash match. With
// verifyOnly it installs nothing and only checks that the patch is in place.
function applyPinnedPatch({
  serverRoot,
  packageName,
  targetPath,
  originalSha256,
  patchedSource,
  installedModuleName,
  sourcePath,
  verifyOnly,
  label,
}) {
  // Resolve from the actual serving CLI, never the validator's /cube/conf tree.
  const serverEntry = realpathSync(join(serverRoot, 'node_modules/.bin/cubejs-server'));
  const servingRequire = createRequire(serverEntry);
  const packagePath = servingRequire.resolve(`${packageName}/package.json`);
  const expectedPackage = resolve(serverRoot, `node_modules/${packageName}/package.json`);
  assert.equal(realpathSync(packagePath), realpathSync(expectedPackage), 'Serving Cube package resolution drifted');
  const version = JSON.parse(readFileSync(packagePath, 'utf8')).version;
  assert.equal(version, PINNED_VERSION, `Review the ${label} patch before upgrading Cube`);
  const target = join(dirname(packagePath), targetPath);
  const installedModule = join(dirname(target), installedModuleName);
  const originalHash = sha256(readFileSync(target));
  const patchedHash = sha256(patchedSource);
  const moduleSource = readFileSync(sourcePath);
  if (originalHash === originalSha256 && !verifyOnly) {
    writeFileSync(installedModule, moduleSource);
    writeFileSync(target, patchedSource);
  } else {
    assert.equal(originalHash, patchedHash, `Pinned ${label} content drifted or patch is missing`);
    assert.equal(sha256(readFileSync(installedModule)), sha256(moduleSource), `Installed ${label} module content drifted`);
  }
  assert.equal(sha256(readFileSync(target)), patchedHash, `${label} patch installation failed`);
  assert.equal(sha256(readFileSync(installedModule)), sha256(moduleSource), `${label} module installation failed`);
  return {
    servingRequire,
    target,
    result: { version, targetHash: patchedHash, moduleHash: sha256(moduleSource) },
  };
}

function runPatchCli(scriptName, patch, successMessage) {
  const args = process.argv.slice(2);
  const options = {};
  while (args.length) {
    const arg = args.shift();
    if (arg === '--verify') options.verifyOnly = true;
    else if (arg === '--server-root' && args[0]) options.serverRoot = args.shift();
    else throw new Error(`Usage: ${scriptName} [--verify] [--server-root PATH]`);
  }
  patch(options);
  process.stdout.write(`${successMessage}\n`);
}

module.exports = { applyPinnedPatch, runPatchCli, PINNED_VERSION, sha256 };
