// Session-only automatic interruption. Speech decisions come from the local VAD endpoint.
window.VoiceActivity = (() => {
    const RATE = 16000;
    const FRAME_SAMPLES = 5120;
    const PRE_ROLL_SAMPLES = 20800;
    const REQUEST_TIMEOUT_MS = 1800;
    const COOLDOWN_MS = 2500;

    function join(parts) {
        const result = new Int16Array(parts.reduce((count, part) => count + part.length, 0));
        let offset = 0;
        for (const part of parts) { result.set(part, offset); offset += part.length; }
        return result;
    }

    function pcmBytes(samples) {
        const bytes = new Uint8Array(samples.length * 2);
        const view = new DataView(bytes.buffer);
        samples.forEach((sample, index) => view.setInt16(index * 2, sample, true));
        return bytes;
    }

    function wav(samples) {
        const bytes = new Uint8Array(44 + samples.length * 2);
        const view = new DataView(bytes.buffer);
        const label = (offset, text) => [...text].forEach((letter, index) => view.setUint8(offset + index, letter.charCodeAt(0)));
        label(0, 'RIFF'); view.setUint32(4, bytes.length - 8, true); label(8, 'WAVE');
        label(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
        view.setUint16(22, 1, true); view.setUint32(24, RATE, true); view.setUint32(28, RATE * 2, true);
        view.setUint16(32, 2, true); view.setUint16(34, 16, true);
        label(36, 'data'); view.setUint32(40, samples.length * 2, true);
        bytes.set(pcmBytes(samples), 44);
        return new Blob([bytes], {type: 'audio/wav'});
    }

    // Keep the fractional source position between worklet blocks, including at 44.1 kHz.
    function resampler(sourceRate) {
        const quantize = value => {
            const bounded = Math.max(-1, Math.min(1, value));
            return Math.round(bounded * (bounded < 0 ? 32768 : 32767));
        };
        if (sourceRate === RATE) return input => Int16Array.from(input, quantize);
        let carry = new Float32Array(0), position = 0;
        const step = sourceRate / RATE;
        return input => {
            const samples = new Float32Array(carry.length + input.length);
            samples.set(carry); samples.set(input, carry.length);
            const output = [];
            while (position + 1 < samples.length) {
                const index = Math.floor(position), fraction = position - index;
                output.push(quantize(samples[index] * (1 - fraction) + samples[index + 1] * fraction));
                position += step;
            }
            const consumed = Math.min(samples.length, Math.floor(position));
            carry = samples.slice(consumed);
            position -= consumed;
            return Int16Array.from(output);
        };
    }

    function create({getContext = () => ({}), onState = () => {}, onSpeech = () => {}} = {}) {
        let enabled = false, speaking = false, generation = 0;
        let snapshot = null, resource = null, handedCapture = null, pending = null;
        let starting = false, cooldownTimer = null, lastTrigger = -Infinity;
        let phase = 'off', destroyed = false;

        function emit(next, message) {
            phase = next;
            try { onState({enabled, monitoring: next === 'listening', captureActive: Boolean(handedCapture), phase: next, message}); }
            catch (err) { console.warn('Automatic interruption status could not be shown:', err); }
        }

        function readContext() {
            const context = getContext() || {};
            const sessionId = Number(context.sessionId);
            return {sessionId: Number.isSafeInteger(sessionId) && sessionId > 0 ? sessionId : null,
                datasetEpoch: context.datasetEpoch || null, companionId: context.companionId ?? null};
        }

        function contextMatches() {
            let current;
            try { current = readContext(); }
            catch { disable('The conversation could not be checked. Turn automatic interruption on again.', true); return false; }
            // A new chat may receive its first saved session only after this foreground opt-in.
            if (snapshot?.sessionId === null && current.sessionId &&
                snapshot.datasetEpoch === current.datasetEpoch && snapshot.companionId === current.companionId) snapshot = current;
            if (!snapshot || current.sessionId !== snapshot.sessionId || current.datasetEpoch !== snapshot.datasetEpoch ||
                current.companionId !== snapshot.companionId) {
                disable('The conversation or data changed. Turn automatic interruption on again.', true);
                return false;
            }
            return true;
        }

        function cleanup(capture) {
            if (!capture || capture.closed) return;
            capture.closed = true;
            let failed = false;
            try { if (capture.node) capture.node.port.onmessage = null; } catch { failed = true; }
            for (const node of [capture.source, capture.node, capture.gain]) {
                try { node?.disconnect(); } catch { failed = true; }
            }
            for (const track of capture.stream.getTracks()) {
                try { track.stop(); } catch { failed = true; }
            }
            try { capture.context?.close().catch(() => {}); } catch { failed = true; }
            return failed;
        }

        function cancelPending() {
            const request = pending;
            pending = null;
            if (!request) return false;
            clearTimeout(request.timer);
            try { request.controller.abort(); return false; } catch { return true; }
        }

        function stopMonitoring() {
            generation++;
            starting = false;
            if (cooldownTimer !== null) clearTimeout(cooldownTimer);
            cooldownTimer = null;
            const cancelFailed = cancelPending();
            const capture = resource;
            resource = null;
            return cleanup(capture) || cancelFailed;
        }

        function disable(reason = 'Automatic interruption is off.', error = false) {
            enabled = false;
            const failed = stopMonitoring();
            const capture = handedCapture;
            handedCapture = null;
            const captureFailed = cleanup(capture);
            emit(error || failed || captureFailed ? 'error' : 'off', failed || captureFailed
                ? 'Microphone cleanup could not be confirmed. Close this page to release the microphone.' : reason);
        }

        function suspend(reason = 'Automatic interruption paused for recording. Turn it on again to resume.') {
            enabled = false;
            const failed = stopMonitoring();
            // An automatic handoff owns its stream until the recording finishes or is cancelled.
            emit(failed ? 'error' : handedCapture ? 'capturing' : 'off', failed
                ? 'Microphone cleanup could not be confirmed. Close this page to release the microphone.' : reason);
        }

        function waiting() {
            if (enabled && !handedCapture) emit('waiting', snapshot?.sessionId
                ? 'Automatic interruption is on. The microphone opens only while Altera speaks.'
                : 'Automatic interruption is on. Waiting for this conversation to start.');
        }

        function captureEnded(capture) {
            if (handedCapture === capture) handedCapture = null;
            if (cleanup(capture)) {
                disable('Microphone cleanup could not be confirmed. Close this page to release the microphone.', true);
            } else if (!enabled) emit('off', 'Automatic interruption is off.');
            else if (speaking) arm();
            else waiting();
        }

        function handoff(capture) {
            if (capture !== resource || !enabled || !speaking || !contextMatches()) return;
            lastTrigger = Date.now();
            generation++;
            cancelPending();
            resource = null;
            handedCapture = capture;
            capture.mode = 'capturing';
            const prefixPCM = join(capture.candidate || []);
            capture.recorded = [prefixPCM.slice()];
            capture.candidate = null;
            let finished = null;
            const recording = {
                finish() {
                    if (finished) return finished;
                    if (capture.closed) throw new Error('This automatic recording was cancelled.');
                    finished = wav(join(capture.recorded));
                    capture.recorded = [];
                    captureEnded(capture);
                    return finished;
                },
                cancel() {
                    capture.recorded = [];
                    captureEnded(capture);
                },
            };
            emit('capturing', 'Your voice interrupted Altera. Recording continues until you stop the microphone.');
            try {
                Promise.resolve(onSpeech({stream: capture.stream, prefixPCM, sampleRate: RATE, capture: recording}))
                    .then(accepted => {
                        if (accepted === false && handedCapture === capture) disable('Recording could not start. Use the microphone button.', true);
                    })
                    .catch(() => {
                        if (handedCapture === capture) disable('Recording could not start. Use the microphone button.', true);
                    });
            } catch { disable('Recording could not start. Use the microphone button.', true); }
        }

        async function query(capture, frame) {
            if (!enabled || !speaking || capture !== resource || pending || !contextMatches()) return;
            if (navigator.onLine === false) { disable('Automatic interruption is off while offline.', true); return; }
            const token = generation;
            const request = {controller: new AbortController(), prefix: capture.candidate ? null : join(capture.rolling), tail: []};
            pending = request;
            request.timer = setTimeout(() => {
                if (pending === request) disable('The local speech detector timed out. Turn automatic interruption on again.', true);
            }, REQUEST_TIMEOUT_MS);
            try {
                const headers = {'Content-Type': 'audio/pcm', 'X-Audio-Sample-Rate': String(RATE)};
                if (snapshot.datasetEpoch) headers['X-Altera-Dataset'] = snapshot.datasetEpoch;
                const response = await fetch(`/api/voice/activity?session_id=${snapshot.sessionId}`, {
                    method: 'POST', headers, body: pcmBytes(frame), signal: request.controller.signal,
                });
                if (token !== generation || pending !== request) return;
                if (!response.ok) throw new Error(`The local speech detector is unavailable (HTTP ${response.status}).`);
                const data = await response.json();
                if (token !== generation || pending !== request || !contextMatches()) return;
                if (data.available !== true || typeof data.speech !== 'boolean' || data.sample_rate !== RATE ||
                    data.samples !== frame.length || Number(data.session_id) !== snapshot.sessionId ||
                    String(data.avatar_id ?? '') !== String(snapshot.companionId ?? '')) {
                    throw new Error('The local speech detector returned an invalid or changed conversation.');
                }
                if (data.speech) {
                    capture.positives++;
                    if (!capture.candidate) capture.candidate = [request.prefix, ...request.tail];
                    if (capture.positives >= 2) handoff(capture);
                } else {
                    capture.positives = 0;
                    capture.candidate = null;
                }
            } catch (err) {
                if (token === generation && pending === request) disable(`${err.message} Use the microphone button or turn automatic interruption on again.`, true);
            } finally {
                clearTimeout(request.timer);
                if (pending === request) {
                    pending = null;
                    const next = capture.latestFrame;
                    capture.latestFrame = null;
                    if (next && resource === capture && enabled && speaking) query(capture, next);
                }
            }
        }

        function samples(capture, input, sourceRate) {
            if (capture.closed) return;
            if (!Number.isFinite(sourceRate) || sourceRate < 8000 || sourceRate > 192000 || sourceRate !== capture.context.sampleRate) {
                disable('The microphone sample rate could not be checked. Use the microphone button.', true); return;
            }
            const pcm = capture.convert(input);
            if (!pcm.length) return;
            if (capture.mode === 'capturing') { capture.recorded.push(pcm); return; }
            if (capture !== resource || !enabled || !speaking || !contextMatches()) return;
            capture.rolling.push(pcm);
            capture.rollingSamples += pcm.length;
            while (capture.rollingSamples > PRE_ROLL_SAMPLES) {
                const excess = capture.rollingSamples - PRE_ROLL_SAMPLES;
                if (capture.rolling[0].length <= excess) capture.rollingSamples -= capture.rolling.shift().length;
                else { capture.rolling[0] = capture.rolling[0].slice(excess); capture.rollingSamples -= excess; }
            }
            if (capture.candidate) capture.candidate.push(pcm);
            else if (pending?.prefix) pending.tail.push(pcm);
            let offset = 0;
            while (offset < pcm.length) {
                const count = Math.min(pcm.length - offset, FRAME_SAMPLES - capture.frameUsed);
                capture.frame.set(pcm.subarray(offset, offset + count), capture.frameUsed);
                offset += count; capture.frameUsed += count;
                if (capture.frameUsed === FRAME_SAMPLES) {
                    const frame = capture.frame;
                    capture.frame = new Int16Array(FRAME_SAMPLES); capture.frameUsed = 0;
                    if (pending) capture.latestFrame = frame;
                    else query(capture, frame);
                }
            }
        }

        async function arm() {
            if (!enabled || !speaking || resource || handedCapture || starting || !contextMatches()) return false;
            if (document.hidden) { disable('Automatic interruption is off while this page is in the background.'); return false; }
            if (!snapshot.sessionId) { waiting(); return false; }
            if (navigator.onLine === false) { disable('Automatic interruption is off while offline.', true); return false; }
            const remaining = COOLDOWN_MS - (Date.now() - lastTrigger);
            if (remaining > 0) {
                if (cooldownTimer === null) {
                    const token = generation;
                    cooldownTimer = setTimeout(() => {
                        cooldownTimer = null;
                        if (token === generation) arm();
                    }, remaining);
                }
                emit('cooldown', 'Automatic interruption is pausing briefly after your last interruption.');
                return false;
            }
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!navigator.mediaDevices?.getUserMedia || !AudioContext || typeof AudioWorkletNode !== 'function') {
                disable('Automatic interruption needs AudioWorklet microphone support. Use the microphone button.', true); return false;
            }
            starting = true;
            const token = generation;
            let capture = null, stream = null;
            emit('starting', 'Opening the microphone for automatic interruption. Headphones help avoid speaker echo.');
            try {
                const echoSupported = Boolean(navigator.mediaDevices.getSupportedConstraints?.().echoCancellation);
                stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, sampleRate: RATE,
                    echoCancellation: echoSupported ? {exact: true} : true, noiseSuppression: true}});
                if (token !== generation || !enabled || !speaking) { cleanup({stream}); return false; }
                if (!contextMatches()) { cleanup({stream}); return false; }
                if (echoSupported && stream.getAudioTracks().some(track => track.getSettings?.().echoCancellation === false)) {
                    throw new Error('This microphone did not enable echo cancellation.');
                }
                const context = new AudioContext({sampleRate: RATE});
                capture = {stream, context, mode: 'monitoring', rolling: [], rollingSamples: 0, positives: 0,
                    candidate: null, frame: new Int16Array(FRAME_SAMPLES), frameUsed: 0, latestFrame: null,
                    convert: resampler(context.sampleRate)};
                resource = capture;
                if (!context.audioWorklet?.addModule) throw new Error('AudioWorklet capture is unavailable.');
                await context.audioWorklet.addModule('/static/js/voice-capture-worklet.js');
                if (token !== generation || resource !== capture) { cleanup(capture); return false; }
                capture.source = context.createMediaStreamSource(stream);
                capture.node = new AudioWorkletNode(context, 'altera-voice-capture', {numberOfInputs: 1, numberOfOutputs: 1,
                    outputChannelCount: [1], channelCount: 1, channelCountMode: 'explicit'});
                capture.gain = context.createGain(); capture.gain.gain.value = 0;
                capture.node.port.onmessage = event => {
                    if (event.data?.samples instanceof Float32Array) samples(capture, event.data.samples, event.data.sampleRate);
                };
                capture.node.addEventListener('processorerror', () => {
                    if (resource === capture || handedCapture === capture) disable('Microphone capture failed. Use the microphone button.', true);
                });
                for (const track of stream.getAudioTracks()) track.addEventListener?.('ended', () => {
                    if (resource === capture || handedCapture === capture) disable('The microphone disconnected. Use the microphone button.', true);
                }, {once: true});
                capture.source.connect(capture.node); capture.node.connect(capture.gain); capture.gain.connect(context.destination);
                await context.resume();
                if (token !== generation || resource !== capture) { cleanup(capture); return false; }
                if (context.state !== 'running') throw new Error('The microphone audio context could not start.');
                emit('listening', 'Listening for your voice with the local speech detector. Headphones help avoid speaker echo.');
                return true;
            } catch (err) {
                cleanup(capture || (stream ? {stream} : null));
                if (token === generation) disable(`${err.message} Use the microphone button.`, true);
                return false;
            } finally { if (token === generation) starting = false; }
        }

        async function enable() {
            if (destroyed) return false;
            if (document.hidden) { disable('Open this page before turning automatic interruption on.'); return false; }
            if (enabled) return true;
            try { snapshot = readContext(); }
            catch { disable('The conversation could not be checked. Use the microphone button.', true); return false; }
            enabled = true;
            waiting();
            if (speaking) return arm();
            return true;
        }

        function setSpeaking(value) {
            if (destroyed) return;
            speaking = Boolean(value);
            if (!speaking) {
                if (stopMonitoring()) { disable('Microphone cleanup could not be confirmed. Close this page.', true); return; }
                waiting();
            } else if (enabled) arm();
        }

        const background = () => { if (document.hidden) disable('Automatic interruption is off after leaving this page. Turn it on again to resume.'); };
        const leave = () => disable('Automatic interruption is off after navigation.');
        const offline = () => disable('Automatic interruption is off while offline.', true);
        const restored = event => { if (event.persisted) disable('Automatic interruption is off after restoring this page.'); };
        document.addEventListener('visibilitychange', background);
        document.addEventListener('freeze', leave);
        window.addEventListener('pagehide', leave);
        window.addEventListener('pageshow', restored);
        window.addEventListener('offline', offline);
        const api = {
            enable, disable, suspend, setSpeaking,
            contextChanged: reason => disable(reason || 'Automatic interruption is off after the conversation or data changed.'),
            getState: () => ({enabled, monitoring: phase === 'listening', captureActive: Boolean(handedCapture), phase}),
            destroy() {
                disable();
                destroyed = true;
                document.removeEventListener('visibilitychange', background);
                document.removeEventListener('freeze', leave);
                window.removeEventListener('pagehide', leave);
                window.removeEventListener('pageshow', restored);
                window.removeEventListener('offline', offline);
            },
        };
        emit('off', 'Automatic interruption is off.');
        return Object.freeze(api);
    }

    return Object.freeze({create});
})();
