// Capture local microphone samples without sending microphone audio to the speakers.
class AlteraVoiceCaptureProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this.samples = new Float32Array(1024);
        this.used = 0;
    }

    process(inputs, outputs) {
        for (const output of outputs) for (const channel of output) channel.fill(0);
        const input = inputs[0]?.[0];
        if (!input) return true;
        let offset = 0;
        while (offset < input.length) {
            const count = Math.min(input.length - offset, this.samples.length - this.used);
            this.samples.set(input.subarray(offset, offset + count), this.used);
            this.used += count;
            offset += count;
            if (this.used === this.samples.length) {
                const samples = this.samples;
                this.port.postMessage({samples, sampleRate}, [samples.buffer]);
                this.samples = new Float32Array(1024);
                this.used = 0;
            }
        }
        return true;
    }
}

registerProcessor('altera-voice-capture', AlteraVoiceCaptureProcessor);
