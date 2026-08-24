#!/usr/bin/env node

import { pathToFileURL } from "node:url";

function sdkSpecifier() {
	const configured = process.env.GOAL_PLUS_AGENT_POSIX_SDK_ENTRY;
	if (!configured) return "@thinkthread/agent-posix";
	return configured.startsWith("file:") ? configured : pathToFileURL(configured).href;
}

function errorPayload(error) {
	const payload = {
		name: error instanceof Error ? error.name : "UnknownError",
		message: error instanceof Error ? error.message : String(error),
	};
	if (error && typeof error === "object") {
		for (const key of [
			"category",
			"delivery",
			"method",
			"path",
			"constraint",
			"step",
			"requestId",
		]) {
			if (error[key] !== undefined) payload[key] = error[key];
		}
		const response = error.response;
		if (response && typeof response === "object") payload.response = response;
		const rejection = error.rejection;
		if (rejection && typeof rejection === "object") payload.rejection = rejection;
		if (error.cause && typeof error.cause === "object") {
			payload.cause = errorPayload(error.cause);
		}
	}
	return payload;
}

async function readRequest() {
	let text = "";
	process.stdin.setEncoding("utf8");
	for await (const chunk of process.stdin) text += chunk;
	const parsed = JSON.parse(text || "null");
	if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
		throw new TypeError("bridge request must be an object");
	}
	if (typeof parsed.operation !== "string" || parsed.operation.length === 0) {
		throw new TypeError("bridge request operation must be a non-empty string");
	}
	if (
		parsed.params !== undefined &&
		(!parsed.params || typeof parsed.params !== "object" || Array.isArray(parsed.params))
	) {
		throw new TypeError("bridge request params must be an object");
	}
	return parsed;
}

try {
	const request = await readRequest();
	const sdk = await import(sdkSpecifier());
	if (request.operation === "bridge.meta") {
		process.stdout.write(
			`${JSON.stringify({
				ok: true,
				result: {
					contractFingerprint: sdk.CONTRACT_FINGERPRINT,
					controlProtocolVersion: sdk.CONTROL_PROTOCOL_VERSION,
					methods: sdk.METHODS,
				},
			})}\n`,
		);
	} else {
		const client = sdk.AgentPosixClient.fromEnv();
		let result;
		if (request.operation === "workflow.fs.snapshot.patchBytes") {
			const params = request.params ?? {};
			if (!Array.isArray(params.mutations)) {
				throw new TypeError("patchBytes mutations must be an array");
			}
			const mutations = params.mutations.map((mutation) => {
				if (!mutation || typeof mutation !== "object") {
					throw new TypeError("patchBytes mutation must be an object");
				}
				if (mutation.kind !== "put_file") return mutation;
				if (typeof mutation.dataBase64 !== "string") {
					throw new TypeError("patchBytes put_file requires dataBase64");
				}
				return {
					kind: "put_file",
					path: mutation.path,
					bytes: Buffer.from(mutation.dataBase64, "base64"),
					...(mutation.mode === undefined ? {} : { mode: mutation.mode }),
				};
			});
			result = await new sdk.FsWorkflows(client).patchBytes(
				params.snapshotId,
				params.requestId,
				mutations,
			);
		} else {
			if (!Array.isArray(sdk.METHODS) || !sdk.METHODS.includes(request.operation)) {
				throw new TypeError(`unsupported Agent POSIX operation: ${request.operation}`);
			}
			result = await client.invoke(request.operation, request.params ?? {});
		}
		process.stdout.write(`${JSON.stringify({ ok: true, result })}\n`);
	}
} catch (error) {
	process.stdout.write(`${JSON.stringify({ ok: false, error: errorPayload(error) })}\n`);
	process.exitCode = 1;
}
