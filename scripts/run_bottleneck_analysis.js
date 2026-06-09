'use strict';

const grpc = require('@grpc/grpc-js');
const { connect, hash, signers } = require('@hyperledger/fabric-gateway');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');

const execFileAsync = promisify(execFile);

const ROOT = path.resolve(__dirname, '..');
const WORKSPACE_ROOT = path.resolve(ROOT, '..');
const DEFAULT_OUTPUT_ROOT = path.join(ROOT, 'results', 'bottleneck');
const TOP_LEVEL_RESULTS_ROOT = path.join(WORKSPACE_ROOT, 'paper-results', 'bottleneck-analysis');

const CHANNEL_NAME = 'mychannel';
const CHAINCODE_NAME = 'basic';
const MSP_ID = 'Org1MSP';
const PEER_ENDPOINT = 'localhost:7051';
const PEER_HOST_ALIAS = 'peer0.org1.example.com';

const CRYPTO_BASE = path.resolve(
    ROOT,
    '..',
    'fabric-samples',
    'test-network',
    'organizations',
    'peerOrganizations',
    'org1.example.com'
);

const KEY_DIRECTORY_PATH = path.join(
    CRYPTO_BASE,
    'users',
    'Admin@org1.example.com',
    'msp',
    'keystore'
);

const CERT_DIRECTORY_PATH = path.join(
    CRYPTO_BASE,
    'users',
    'Admin@org1.example.com',
    'msp',
    'signcerts'
);

const TLS_CERT_PATH = path.join(
    CRYPTO_BASE,
    'peers',
    'peer0.org1.example.com',
    'tls',
    'ca.crt'
);

const MONITORED_CONTAINERS = [
    'peer0.org1.example.com',
    'peer0.org2.example.com',
    'orderer.example.com',
];

function parseArgs(argv) {
    const args = {
        tps: [50, 100, 150, 200, 250],
        duration: 8,
        payloadKb: 1,
        outputDir: '',
        statsIntervalMs: 1000,
        logTailLines: 200,
    };

    for (let i = 0; i < argv.length; i++) {
        const arg = argv[i];
        if (arg === '--tps') {
            args.tps = [];
            while (argv[i + 1] && !argv[i + 1].startsWith('--')) {
                args.tps.push(Number(argv[++i]));
            }
        } else if (arg === '--duration') {
            args.duration = Number(argv[++i]);
        } else if (arg === '--payload-kb') {
            args.payloadKb = Number(argv[++i]);
        } else if (arg === '--output-dir') {
            args.outputDir = argv[++i];
        } else if (arg === '--stats-interval-ms') {
            args.statsIntervalMs = Number(argv[++i]);
        } else if (arg === '--log-tail-lines') {
            args.logTailLines = Number(argv[++i]);
        }
    }

    return args;
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function formatTimestamp(date = new Date()) {
    const pad = value => String(value).padStart(2, '0');
    return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

async function ensureDir(dirPath) {
    await fs.mkdir(dirPath, { recursive: true });
}

function csvEscape(value) {
    if (value === null || value === undefined) {
        return '';
    }

    const stringValue = String(value);
    if (/[",\n]/.test(stringValue)) {
        return `"${stringValue.replace(/"/g, '""')}"`;
    }

    return stringValue;
}

async function writeCsv(filePath, rows, headers) {
    const lines = [headers.join(',')];
    for (const row of rows) {
        lines.push(headers.map(header => csvEscape(row[header])).join(','));
    }
    await fs.writeFile(filePath, `${lines.join('\n')}\n`, 'utf8');
}

async function getFirstDirFileName(dirPath) {
    const files = await fs.readdir(dirPath);
    const file = files[0];
    if (!file) {
        throw new Error(`No files found in directory: ${dirPath}`);
    }
    return path.join(dirPath, file);
}

async function newGrpcConnection() {
    const tlsRootCert = await fs.readFile(TLS_CERT_PATH);
    const tlsCredentials = grpc.credentials.createSsl(tlsRootCert);
    return new grpc.Client(PEER_ENDPOINT, tlsCredentials, {
        'grpc.ssl_target_name_override': PEER_HOST_ALIAS,
    });
}

async function newIdentity() {
    const certPath = await getFirstDirFileName(CERT_DIRECTORY_PATH);
    const credentials = await fs.readFile(certPath);
    return { mspId: MSP_ID, credentials };
}

async function newSigner() {
    const keyPath = await getFirstDirFileName(KEY_DIRECTORY_PATH);
    const privateKeyPem = await fs.readFile(keyPath);
    const privateKey = crypto.createPrivateKey(privateKeyPem);
    return signers.newPrivateKeySigner(privateKey);
}

async function newContract() {
    const client = await newGrpcConnection();
    const gateway = connect({
        client,
        identity: await newIdentity(),
        signer: await newSigner(),
        hash: hash.sha256,
        evaluateOptions: () => ({ deadline: Date.now() + 5000 }),
        endorseOptions: () => ({ deadline: Date.now() + 15000 }),
        submitOptions: () => ({ deadline: Date.now() + 10000 }),
        commitStatusOptions: () => ({ deadline: Date.now() + 60000 }),
    });

    const network = gateway.getNetwork(CHANNEL_NAME);
    const contract = network.getContract(CHAINCODE_NAME);
    return { client, gateway, contract };
}

function generatePayload(sizeKb) {
    return 'X'.repeat(sizeKb * 1024);
}

async function runDockerStatsSample(targetTps) {
    const format = '{{json .}}';
    const { stdout } = await execFileAsync('docker', ['stats', '--no-stream', '--format', format, ...MONITORED_CONTAINERS], {
        cwd: WORKSPACE_ROOT,
        maxBuffer: 1024 * 1024 * 4,
    });

    const timestamp = new Date().toISOString();
    return stdout
        .trim()
        .split('\n')
        .filter(Boolean)
        .map(line => JSON.parse(line))
        .map(sample => ({
            timestamp,
            target_tps: targetTps,
            container: sample.Name,
            container_id: sample.ID,
            cpu_percent: sample.CPUPerc,
            memory_usage: sample.MemUsage,
            memory_percent: sample.MemPerc,
            net_io: sample.NetIO,
            block_io: sample.BlockIO,
            pids: sample.PIDs,
        }));
}

async function collectDockerStats(runState) {
    while (!runState.stopped) {
        try {
            const samples = await runDockerStatsSample(runState.targetTps);
            runState.resourceMetrics.push(...samples);
        } catch (error) {
            runState.resourceErrors.push({
                timestamp: new Date().toISOString(),
                target_tps: runState.targetTps,
                error: error.message,
            });
        }

        await sleep(runState.statsIntervalMs);
    }
}

async function collectContainerLogs(runDir, startIso, endIso) {
    const logDir = path.join(runDir, 'container-logs');
    await ensureDir(logDir);

    for (const container of MONITORED_CONTAINERS) {
        const { stdout, stderr } = await execFileAsync(
            'docker',
            ['logs', '--since', startIso, '--until', endIso, '--tail', 'all', container],
            { cwd: WORKSPACE_ROOT, maxBuffer: 1024 * 1024 * 20 }
        );

        const combined = [stdout, stderr].filter(Boolean).join('\n');
        await fs.writeFile(path.join(logDir, `${container}.log`), combined, 'utf8');
    }
}

async function collectLogTail(runDir, lineCount) {
    const logDir = path.join(runDir, 'container-logs');
    await ensureDir(logDir);

    for (const container of MONITORED_CONTAINERS) {
        const { stdout, stderr } = await execFileAsync(
            'docker',
            ['logs', '--tail', String(lineCount), container],
            { cwd: WORKSPACE_ROOT, maxBuffer: 1024 * 1024 * 20 }
        );
        const combined = [stdout, stderr].filter(Boolean).join('\n');
        await fs.writeFile(path.join(logDir, `${container}.tail.log`), combined, 'utf8');
    }
}

async function submitTimedTransaction(contract, targetTps, sequenceNumber, payloadKb) {
    const assetId = `bottleneck_${targetTps}_${sequenceNumber}_${Date.now()}_${Math.floor(Math.random() * 1000000)}`;
    const payload = generatePayload(payloadKb);
    const result = {
        target_tps: targetTps,
        sequence_number: sequenceNumber,
        asset_id: assetId,
        status: 'success',
        error_message: '',
        t1_proposal_sent_ms: Date.now(),
        t2_endorsement_received_ms: '',
        t3_submitted_to_orderer_ms: '',
        t4_committed_ms: '',
        endorsement_delay_ms: '',
        ordering_delay_ms: '',
        commit_delay_ms: '',
        total_latency_ms: '',
        transaction_id: '',
    };

    try {
        const proposal = contract.newProposal('CreateAsset', {
            arguments: [assetId, 'blue', '10', 'Tanmay', '1000', payload],
        });

        const endorsed = await proposal.endorse();
        result.t2_endorsement_received_ms = Date.now();

        const commit = await endorsed.submit();
        result.t3_submitted_to_orderer_ms = Date.now();
        result.transaction_id = commit.getTransactionId();

        const status = await commit.getStatus();
        result.t4_committed_ms = Date.now();

        if (!status.successful) {
            result.status = 'failed';
            result.error_message = `Commit status code ${String(status.code)}`;
        }
    } catch (error) {
        result.status = 'failed';
        result.error_message = error.message;
        const failureTime = Date.now();
        if (!result.t2_endorsement_received_ms) {
            result.t2_endorsement_received_ms = failureTime;
        }
        if (!result.t3_submitted_to_orderer_ms) {
            result.t3_submitted_to_orderer_ms = failureTime;
        }
        if (!result.t4_committed_ms) {
            result.t4_committed_ms = failureTime;
        }
    }

    const t1 = Number(result.t1_proposal_sent_ms);
    const t2 = Number(result.t2_endorsement_received_ms);
    const t3 = Number(result.t3_submitted_to_orderer_ms);
    const t4 = Number(result.t4_committed_ms);

    result.endorsement_delay_ms = t2 - t1;
    result.ordering_delay_ms = t3 - t2;
    result.commit_delay_ms = t4 - t3;
    result.total_latency_ms = t4 - t1;

    return result;
}

async function runLoadPoint(contract, options) {
    const { targetTps, durationSeconds, payloadKb, runDir, statsIntervalMs, logTailLines } = options;
    const totalTransactions = Math.ceil(targetTps * durationSeconds);
    const intervalMs = 1000 / targetTps;
    const stageTimings = [];
    const inflight = [];

    const runState = {
        stopped: false,
        targetTps,
        statsIntervalMs,
        resourceMetrics: [],
        resourceErrors: [],
    };

    const statsPromise = collectDockerStats(runState);

    const startDate = new Date();
    const startHr = process.hrtime.bigint();

    for (let sequence = 0; sequence < totalTransactions; sequence++) {
        const expectedElapsedMs = Math.round(sequence * intervalMs);
        while (true) {
            const nowElapsedMs = Number((process.hrtime.bigint() - startHr) / 1000000n);
            const remainingMs = expectedElapsedMs - nowElapsedMs;
            if (remainingMs <= 0) {
                break;
            }
            await sleep(Math.min(remainingMs, 5));
        }

        inflight.push(
            submitTimedTransaction(contract, targetTps, sequence, payloadKb)
                .then(result => {
                    stageTimings.push(result);
                })
        );
    }

    await Promise.all(inflight);
    runState.stopped = true;
    await statsPromise;

    const endDate = new Date();

    await writeCsv(
        path.join(runDir, 'stage_timings.csv'),
        stageTimings,
        [
            'target_tps',
            'sequence_number',
            'asset_id',
            'status',
            'error_message',
            'transaction_id',
            't1_proposal_sent_ms',
            't2_endorsement_received_ms',
            't3_submitted_to_orderer_ms',
            't4_committed_ms',
            'endorsement_delay_ms',
            'ordering_delay_ms',
            'commit_delay_ms',
            'total_latency_ms',
        ]
    );

    await writeCsv(
        path.join(runDir, 'resource_metrics.csv'),
        runState.resourceMetrics,
        [
            'timestamp',
            'target_tps',
            'container',
            'container_id',
            'cpu_percent',
            'memory_usage',
            'memory_percent',
            'net_io',
            'block_io',
            'pids',
        ]
    );

    if (runState.resourceErrors.length > 0) {
        await writeCsv(
            path.join(runDir, 'resource_errors.csv'),
            runState.resourceErrors,
            ['timestamp', 'target_tps', 'error']
        );
    }

    await collectContainerLogs(runDir, startDate.toISOString(), endDate.toISOString());
    await collectLogTail(runDir, logTailLines);

    await fs.writeFile(
        path.join(runDir, 'metadata.json'),
        JSON.stringify(
            {
                target_tps: targetTps,
                duration_seconds: durationSeconds,
                payload_kb: payloadKb,
                total_transactions: totalTransactions,
                start_time: startDate.toISOString(),
                end_time: endDate.toISOString(),
            },
            null,
            2
        ),
        'utf8'
    );

    return {
        target_tps: targetTps,
        run_dir: runDir,
        total_transactions: totalTransactions,
        start_time: startDate.toISOString(),
        end_time: endDate.toISOString(),
    };
}

async function copyTopLevelArtifacts(sourceDir) {
    await ensureDir(TOP_LEVEL_RESULTS_ROOT);
    const destinationDir = path.join(TOP_LEVEL_RESULTS_ROOT, path.basename(sourceDir));
    await ensureDir(destinationDir);

    const files = await fs.readdir(sourceDir);
    for (const file of files) {
        if (!/\.(csv|png|md|json)$/i.test(file)) {
            continue;
        }
        await fs.copyFile(path.join(sourceDir, file), path.join(destinationDir, file));
    }
}

async function main() {
    const args = parseArgs(process.argv.slice(2));
    const outputDir = args.outputDir
        ? path.resolve(ROOT, args.outputDir)
        : path.join(DEFAULT_OUTPUT_ROOT, formatTimestamp());

    await ensureDir(outputDir);

    const manifest = {
        created_at: new Date().toISOString(),
        runs: [],
    };

    const { client, gateway, contract } = await newContract();

    try {
        for (const targetTps of args.tps) {
            const runDir = path.join(outputDir, `tps_${targetTps}`);
            await ensureDir(runDir);
            console.log(`Running bottleneck analysis at ${targetTps} TPS...`);
            const runSummary = await runLoadPoint(contract, {
                targetTps,
                durationSeconds: args.duration,
                payloadKb: args.payloadKb,
                runDir,
                statsIntervalMs: args.statsIntervalMs,
                logTailLines: args.logTailLines,
            });
            manifest.runs.push(runSummary);
        }
    } finally {
        gateway.close();
        client.close();
    }

    await fs.writeFile(path.join(outputDir, 'manifest.json'), JSON.stringify(manifest, null, 2), 'utf8');

    console.log(`Bottleneck raw results written to ${outputDir}`);
    console.log(`Analyze with: python3 ${path.join(ROOT, 'scripts', 'analyze_bottleneck.py')} --input-dir ${outputDir}`);
}

main().catch(error => {
    console.error(error);
    process.exitCode = 1;
});
