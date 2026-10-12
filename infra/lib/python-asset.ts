import { spawnSync } from "node:child_process";
import { cpSync } from "node:fs";
import { join } from "node:path";
import { DockerImage } from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";

/**
 * Package backend/src/portal plus pinned runtime deps (backend/requirements.txt).
 * boto3 is bundled on purpose: the Lambda-provided SDK may lag behind the IAM API
 * fields we rely on (CredentialAgeDays, ServiceCredentialSecret).
 * Bundles locally with `uv` when available; falls back to Docker.
 */
export function pythonCode(backendDir: string, runtime: lambda.Runtime): lambda.Code {
  return lambda.Code.fromAsset(backendDir, {
    exclude: [".venv", "tests", ".pytest_cache", ".ruff_cache", "**/__pycache__", "*.egg-info"],
    bundling: {
      image: runtime.bundlingImage ?? DockerImage.fromRegistry("public.ecr.aws/sam/build-python3.14"),
      platform: "linux/arm64",
      command: [
        "bash",
        "-c",
        "pip install --no-cache-dir --no-deps -r requirements.txt -t /asset-output && cp -r src/portal /asset-output/",
      ],
      local: {
        tryBundle(outputDir: string): boolean {
          const uv = spawnSync("uv", ["--version"], { stdio: "ignore" });
          if (uv.status !== 0) return false;
          const result = spawnSync(
            "uv",
            [
              "pip",
              "install",
              "--quiet",
              "--no-deps",
              "--target",
              outputDir,
              "--python-platform",
              "aarch64-manylinux2014",
              "--python-version",
              "3.14",
              "-r",
              join(backendDir, "requirements.txt"),
            ],
            { stdio: "inherit" },
          );
          if (result.status !== 0) return false;
          cpSync(join(backendDir, "src", "portal"), join(outputDir, "portal"), {
            recursive: true,
            filter: (src) => !src.includes("__pycache__"),
          });
          return true;
        },
      },
    },
  });
}
