// Discover every top-level frontend test, including frontend-reliability.cjs.
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const tests = fs.readdirSync(path.join(root, 'tests'), {withFileTypes: true})
    .filter(entry => entry.isFile() && entry.name.endsWith('.cjs'))
    .map(entry => path.join('tests', entry.name)).sort();
if (!tests.length) {
    process.stderr.write('No frontend tests were found.\n');
    process.exitCode = 1;
} else {
    const result = spawnSync(process.execPath, ['--test', ...tests], {cwd: root, stdio: 'inherit'});
    process.exitCode = result.status ?? 1;
}
