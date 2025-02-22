"""The main entry point for performing comparison on chatbots."""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
from dataclasses import asdict
from typing import cast

import config as chatbot_config
import pandas as pd
from modeling import make_predictions, process_data

from zeno_build.experiments import search_space
from zeno_build.experiments.experiment_run import ExperimentRun
from zeno_build.optimizers import exhaustive
from zeno_build.prompts.chat_prompt import ChatMessages
from zeno_build.reporting import reporting_utils
from zeno_build.reporting.visualize import visualize


def chatbot_main(
    models: list[str],
    single_model: str,
    prompts: list[str],
    single_prompt: str,
    experiments: list[str],
    hf_inference_method: str,
    results_dir: str,
    do_prediction: bool = True,
    do_visualization: bool = True,
):
    """Run the chatbot experiment."""
    # Update the experiment settings with the provided models and prompts
    experiment_settings: list[search_space.CombinatorialSearchSpace] = [
        copy.deepcopy(chatbot_config.experiments[x]) for x in experiments
    ]
    for setting in experiment_settings:
        if isinstance(setting.dimensions["model_preset"], search_space.Categorical):
            setting.dimensions["model_preset"] = search_space.Categorical(models)
        else:
            assert isinstance(setting.dimensions["model_preset"], search_space.Constant)
            setting.dimensions["model_preset"] = search_space.Constant(single_model)
        if isinstance(setting.dimensions["prompt_preset"], search_space.Categorical):
            setting.dimensions["prompt_preset"] = search_space.Categorical(prompts)
        else:
            assert isinstance(
                setting.dimensions["prompt_preset"], search_space.Constant
            )
            setting.dimensions["prompt_preset"] = search_space.Constant(single_prompt)
    my_space = search_space.CompositeSearchSpace(
        cast(list[search_space.SearchSpace], experiment_settings)
    )

    # Get the dataset configuration
    dataset_config = chatbot_config.dataset_configs[chatbot_config.dataset]

    # Define the directories for storing data and predictions
    data_dir = os.path.join(results_dir, "data")
    predictions_dir = os.path.join(results_dir, "predictions")

    # Load and standardize the format of the necessary data. The resulting
    # processed data will be stored in the `results_dir/data` directory
    # both for browsing and for caching for fast reloading on future runs.
    contexts_and_labels: list[ChatMessages] = process_data(
        dataset=dataset_config.dataset,
        split=dataset_config.split,
        data_format=dataset_config.data_format,
        data_column=dataset_config.data_column,
        output_dir=data_dir,
    )

    # Organize the data into labels (output) and context (input)
    labels: list[str] = []
    contexts: list[ChatMessages] = []
    for candl in contexts_and_labels:
        labels.append(candl.messages[-1].content)
        contexts.append(ChatMessages(candl.messages[:-1]))

    if do_prediction:
        # Perform the hyperparameter sweep
        optimizer = exhaustive.ExhaustiveOptimizer(
            space=my_space,
            distill_functions=chatbot_config.sweep_distill_functions,
            metric=chatbot_config.sweep_metric_function,
            num_trials=chatbot_config.num_trials,
        )

        while not optimizer.is_complete(predictions_dir, include_in_progress=True):
            # Get parameters
            parameters = optimizer.get_parameters()
            if parameters is None:
                break
            # Get the run ID and resulting predictions
            id_and_predictions = make_predictions(
                contexts=contexts,
                prompt_preset=parameters["prompt_preset"],
                model_preset=parameters["model_preset"],
                temperature=parameters["temperature"],
                max_tokens=parameters["max_tokens"],
                top_p=parameters["top_p"],
                context_length=parameters["context_length"],
                output_dir=predictions_dir,
                hf_inference_method=hf_inference_method,
            )
            if id_and_predictions is None:
                print(f"*** Skipped run for {parameters=} ***")
                continue
            # Run or read the evaluation result
            id, predictions = id_and_predictions
            if os.path.exists(f"{predictions_dir}/{id}.eval"):
                with open(f"{predictions_dir}/{id}.eval", "r") as f:
                    eval_result = float(next(f).strip())
            else:
                eval_result = optimizer.calculate_metric(contexts, labels, predictions)
                with open(f"{predictions_dir}/{id}.eval", "w") as f:
                    f.write(f"{eval_result}")
            # Print out the results
            print("*** Iteration complete. ***")
            print(f"Eval: {eval_result}, Parameters: {parameters}")
            print("***************************")

    if do_visualization:
        param_files = my_space.get_valid_param_files(
            predictions_dir, include_in_progress=False
        )
        if chatbot_config.num_trials and len(param_files) < chatbot_config.num_trials:
            logging.getLogger().warning(
                "Not enough completed but performing visualization anyway."
            )
        results: list[ExperimentRun] = []
        for param_file in param_files:
            assert param_file.endswith(".zbp")
            with open(param_file, "r") as f:
                loaded_parameters = json.load(f)
            with open(f"{param_file[:-4]}.json", "r") as f:
                predictions = json.load(f)
            name = reporting_utils.parameters_to_name(loaded_parameters, my_space)
            results.append(
                ExperimentRun(
                    parameters=loaded_parameters, predictions=predictions, name=name
                )
            )
        results.sort(key=lambda x: x.name)

        # Perform the visualization
        df = pd.DataFrame(
            {
                "messages": [[asdict(y) for y in x.messages] for x in contexts],
                "label": labels,
            }
        )
        visualize(
            df,
            labels,
            results,
            "openai-chat",
            "messages",
            chatbot_config.zeno_distill_and_metric_functions,
            zeno_config={"cache_path": os.path.join(results_dir, "zeno_cache")},
        )


if __name__ == "__main__":
    # Parse the command line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        type=str,
        nargs="+",
        default=chatbot_config.default_models,
        help="The models to use (for experimental settings with multiple models).",
    )
    parser.add_argument(
        "--single-model",
        type=str,
        default=chatbot_config.default_single_model,
        help="The model to use (for experimental settings with a single model).",
    )
    parser.add_argument(
        "--prompts",
        type=str,
        nargs="+",
        default=chatbot_config.default_prompts,
        help="The prompts to use (for experimental settings with multiple prompts).",
    )
    parser.add_argument(
        "--single-prompt",
        type=str,
        default=chatbot_config.default_single_prompt,
        help="The prompt to use (for experimental settings with a single prompt).",
    )
    parser.add_argument(
        "--experiments",
        type=str,
        nargs="+",
        default=["model", "prompt", "temperature", "context_length"],
        help="The experiments to run.",
    )
    parser.add_argument(
        "--hf-inference-method",
        type=str,
        default="huggingface",
        help="The method used to perform inference on HuggingFace models.",
        choices=["huggingface", "vllm"],
    )
    parser.add_argument(
        "--results-dir",
        type=str,
        default="results",
        help="The directory to store the results in.",
    )
    parser.add_argument(
        "--skip-prediction",
        action="store_true",
        help="Skip prediction and just do visualization.",
    )
    parser.add_argument(
        "--skip-visualization",
        action="store_true",
        help="Skip visualization and just do prediction.",
    )
    args = parser.parse_args()

    if args.skip_prediction and args.skip_visualization:
        raise ValueError(
            "Cannot specify both --skip-prediction and --skip-visualization."
        )

    chatbot_main(
        models=args.models,
        single_model=args.single_model,
        prompts=args.prompts,
        single_prompt=args.single_prompt,
        experiments=args.experiments,
        hf_inference_method=args.hf_inference_method,
        results_dir=args.results_dir,
        do_prediction=not args.skip_prediction,
        do_visualization=not args.skip_visualization,
    )
