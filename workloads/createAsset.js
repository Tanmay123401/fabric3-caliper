'use strict';

const { WorkloadModuleBase } = require('@hyperledger/caliper-core');

class CreateAssetWorkload extends WorkloadModuleBase {

    generatePayload(sizeKB) {
        return 'X'.repeat(sizeKB * 1024);
    }

    async submitTransaction() {

        const assetID =
            'asset_' +
            process.pid +
            '_' +
            Date.now() +
            '_' +
            Math.floor(Math.random() * 1000000);

        const payload =
            this.generatePayload(this.roundArguments.payloadKB || 1);

        await this.sutAdapter.sendRequests({
            contractId: 'basic',
            contractFunction: 'CreateAsset',
            contractArguments: [
                assetID,
                'blue',
                '10',
                'Tanmay',
                '1000',
                payload
            ],
            readOnly: false
        });
    }
}

function createWorkloadModule() {
    return new CreateAssetWorkload();
}

module.exports.createWorkloadModule = createWorkloadModule;