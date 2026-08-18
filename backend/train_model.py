#!/usr/bin/env python3
"""Command-line entry point for training the next-horizon predictor.

    python3 train_model.py                  # train, evaluate, save
    python3 train_model.py --dry-run        # evaluate only, write nothing
    python3 train_model.py --horizon 1      # predict the next session
    python3 train_model.py --real-data      # mark as a real, not simulated, run

The pipeline itself lives in `ml/training.py`. This file stays at the
backend root because it is the documented command and because a training
entry point that must be invoked as a module is a small paper cut repeated
forever.
"""
import sys

from ml import training

if __name__ == "__main__":
    sys.exit(training.main())
