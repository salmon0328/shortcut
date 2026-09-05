"""Preflight for the one dependency that can sink the schedule.

Having an AWS account is not the same as being allowed to call Claude. Model
access is a separate, per-account and per-region opt-in:

    AWS Console -> Amazon Bedrock -> Model access -> enable the Anthropic
    models -> Save. For most accounts this is instant; some get a use-case
    form that takes hours.

Run this before writing any AI code, and again on the demo machine:

    python scripts/check_bedrock.py

It checks four things in order and stops at the first failure, because each
one makes the next meaningless: the SDK is installed, credentials resolve, an
inference profile for our model exists in the region, and a real round-trip
returns text. Nothing here writes anything or costs more than a few tokens.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shortcut.ai.config import (  # noqa: E402
    BEDROCK_MODEL_ID,
    BEDROCK_MODEL_ID_APAC,
    load_settings,
)

TICK, CROSS, DOT = "PASS", "FAIL", "  ->"


def fail(message: str, *fixes: str) -> int:
    print(f"{CROSS}  {message}")
    for fix in fixes:
        print(f"{DOT} {fix}")
    return 1


def main() -> int:
    settings = load_settings()
    print(f"region   {settings.region}")
    print(f"model    {settings.model_id}")
    print(f"profile  {settings.profile or '(default credential chain)'}")
    print()

    # 1. SDK present -------------------------------------------------------
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        return fail(
            "boto3 is not installed.",
            "pip install -r requirements-ai.txt",
        )
    print(f"{TICK}  boto3 {boto3.__version__} installed")

    session_kwargs = {"profile_name": settings.profile} if settings.profile else {}
    try:
        session = boto3.Session(**session_kwargs)
    except BotoCoreError as error:
        return fail(f"could not open an AWS session: {error}")

    # 2. Credentials resolve ----------------------------------------------
    if session.get_credentials() is None:
        return fail(
            "no AWS credentials found.",
            "aws configure sso     (recommended for a shared account)",
            "aws configure         (access key + secret)",
            "or set AWS_PROFILE in .env to a profile you already have",
        )
    try:
        who = session.client("sts", region_name=settings.region).get_caller_identity()
        print(f"{TICK}  credentials resolve: account {who['Account']}")
    except (ClientError, NoCredentialsError, BotoCoreError) as error:
        return fail(f"credentials did not work: {error}")

    # 3. An inference profile for the model exists in this region ----------
    control = session.client("bedrock", region_name=settings.region)
    try:
        profiles = control.list_inference_profiles()["inferenceProfileSummaries"]
    except ClientError as error:
        return fail(
            f"cannot list inference profiles in {settings.region}: {error}",
            "Bedrock may not be enabled in this region, or the IAM role is missing "
            "bedrock:ListInferenceProfiles.",
        )

    ids = {profile["inferenceProfileId"] for profile in profiles}
    haiku = sorted(i for i in ids if "haiku-4-5" in i)
    if settings.model_id in ids:
        print(f"{TICK}  model id resolves in {settings.region}")
    else:
        alternative = BEDROCK_MODEL_ID_APAC if settings.model_id == BEDROCK_MODEL_ID else BEDROCK_MODEL_ID
        hints = [f"MODEL_ID={alternative}   <- try this in .env"] if alternative in ids else []
        hints.extend(f"also available: {i}" for i in haiku)
        return fail(
            f"{settings.model_id} is not available in {settings.region}.",
            *(hints or ["No Claude Haiku 4.5 profile is visible. Enable model access "
                        "in the Bedrock console, then re-run."]),
        )

    # 4. A real round-trip -------------------------------------------------
    runtime = session.client("bedrock-runtime", region_name=settings.region)
    try:
        response = runtime.converse(
            modelId=settings.model_id,
            messages=[{"role": "user", "content": [{"text": "Reply with the word: ready"}]}],
            inferenceConfig={"maxTokens": 16, "temperature": 0.0},
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "")
        if code == "AccessDeniedException":
            return fail(
                "the model id resolves but this account may not call it.",
                "Bedrock console -> Model access -> enable the Anthropic models.",
                "Then check the IAM role allows bedrock:InvokeModel and "
                "bedrock:Converse on this model.",
            )
        return fail(f"the model call failed ({code or 'unknown'}): {error}")

    text = response["output"]["message"]["content"][0]["text"].strip()
    usage = response["usage"]
    print(f"{TICK}  model replied: {text!r}")
    print(f"{TICK}  tokens in={usage['inputTokens']} out={usage['outputTokens']}")
    print()
    print("Bedrock is ready. Set MOCK_MODE=false in .env to use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
