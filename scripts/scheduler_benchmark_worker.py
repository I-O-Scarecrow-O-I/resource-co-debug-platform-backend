import argparse
import json
import time


def burn_cpu(duration_seconds: float, seed: int) -> dict[str, int | float]:
    started = time.perf_counter()
    deadline = started + duration_seconds
    state = seed & 0xFFFFFFFF
    iterations = 0

    while time.perf_counter() < deadline:
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        state ^= state >> 13
        state = (state * 2246822519) & 0xFFFFFFFF
        iterations += 1

    elapsed_seconds = time.perf_counter() - started
    return {
        "requested_seconds": duration_seconds,
        "elapsed_seconds": round(elapsed_seconds, 6),
        "iterations": iterations,
        "checksum": state,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a deterministic CPU-bound task for C-module scheduler development."
    )
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--label", default="benchmark-task")
    args = parser.parse_args()

    if args.duration_seconds <= 0:
        parser.error("--duration-seconds must be greater than zero")

    result = burn_cpu(args.duration_seconds, args.seed)
    output: dict[str, int | float | str] = {**result, "label": args.label}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
