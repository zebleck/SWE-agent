import config
import json
import logging
import os
import together

from collections import defaultdict
from anthropic import Anthropic, AnthropicBedrock, HUMAN_PROMPT, AI_PROMPT
from dataclasses import dataclass, fields
from openai import BadRequestError, OpenAI, AzureOpenAI
from simple_parsing.helpers.serialization.serializable import FrozenSerializable, Serializable
from sweagent.agent.commands import Command
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
    retry_if_not_exception_type,
)
from typing import Optional, Union

logger = logging.getLogger("api_models")


@dataclass(frozen=True)
class ModelArguments(FrozenSerializable):
    """Arguments configuring the model and its behavior."""
    model_name: str
    per_instance_cost_limit: float = 0.0
    total_cost_limit: float = 0.0
    temperature: float = 1.0
    top_p: float = 1.0
    replay_path: str = None
    host_url: str = "localhost:11434"


@dataclass
class APIStats(Serializable):
    total_cost: float = 0
    instance_cost: float = 0
    tokens_sent: int = 0
    tokens_received: int = 0
    api_calls: int = 0

    def __add__(self, other):
        if not isinstance(other, APIStats):
            raise TypeError("Can only add APIStats with APIStats")

        return APIStats(**{
            field.name: getattr(self, field.name) + getattr(other, field.name)
            for field in fields(self)
        })
    def replace(self, other):
        if not isinstance(other, APIStats):
            raise TypeError("Can only replace APIStats with APIStats")

        return APIStats(**{
            field.name: getattr(other, field.name)
            for field in fields(self)
        })


class ContextWindowExceededError(Exception):
    pass


class CostLimitExceededError(Exception):
    pass


class BaseModel:
    MODELS = {}
    SHORTCUTS = {}

    def __init__(self, args: ModelArguments, commands: list[Command]):
        self.args = args
        self.commands = commands
        self.model_metadata = {}
        self.stats = APIStats()

        # Map `model_name` to API-compatible name `api_model`
        self.api_model = (
            self.SHORTCUTS[self.args.model_name]
            if self.args.model_name in self.SHORTCUTS
            else self.args.model_name
        )

        # Map model name to metadata (cost, context info)
        MODELS = {
            **{dest: self.MODELS[src] for dest, src in self.SHORTCUTS.items()},
            **self.MODELS,
        }
        if args.model_name in MODELS:
            self.model_metadata = MODELS[args.model_name]
        elif args.model_name.startswith("ft:"):
            ft_model = args.model_name.split(":")[1]
            self.model_metadata = MODELS[ft_model]
        elif args.model_name.startswith("ollama:"):
            self.api_model = args.model_name.split('ollama:', 1)[1]
            self.model_metadata = self.MODELS[self.api_model]
        elif args.model_name.startswith("azure:"):
            azure_model = args.model_name.split("azure:", 1)[1]
            self.model_metadata = MODELS[azure_model]
        elif args.model_name.startswith("bedrock:"):
            self.api_model = args.model_name.split("bedrock:", 1)[1]
            self.model_metadata = MODELS[self.api_model]
        else:
            raise ValueError(f"Unregistered model ({args.model_name}). Add model name to MODELS metadata to {self.__class__}")

    def reset_stats(self, other: Optional[APIStats] = None):
        if other is None:
            self.stats = APIStats(total_cost=self.stats.total_cost)
            logger.info("Resetting model stats")
        else:
            self.stats = other

    def update_stats(self, input_tokens: int, output_tokens: int) -> float:
        """
        Calculates the cost of a response from the openai API.

        Args:
        input_tokens (int): The number of tokens in the prompt.
        output_tokens (int): The number of tokens in the response.

        Returns:
        float: The cost of the response.
        """
        # Calculate cost and update cost related fields
        cost = (
            self.model_metadata["cost_per_input_token"] * input_tokens
            + self.model_metadata["cost_per_output_token"] * output_tokens
        )
        self.stats.total_cost += cost
        self.stats.instance_cost += cost
        self.stats.tokens_sent += input_tokens
        self.stats.tokens_received += output_tokens
        self.stats.api_calls += 1

        # Log updated cost values to std. out.
        logger.info(
            f"input_tokens={input_tokens:_}, "
            f"output_tokens={output_tokens:_}, "
            f"instance_cost={self.stats.instance_cost:.2f}, "
            f"cost={cost:.2f}"
        )
        logger.info(
            f"total_tokens_sent={self.stats.tokens_sent:_}, "
            f"total_tokens_received={self.stats.tokens_received:_}, "
            f"total_cost={self.stats.total_cost:.2f}, "
            f"total_api_calls={self.stats.api_calls:_}"
        )

        # Check whether total cost or instance cost limits have been exceeded
        if 0 < self.args.total_cost_limit <= self.stats.total_cost:
            logger.warning(
                f"Cost {self.stats.total_cost:.2f} exceeds limit {self.args.total_cost_limit:.2f}"
            )
            raise CostLimitExceededError("Total cost limit exceeded")

        if 0 < self.args.per_instance_cost_limit <= self.stats.instance_cost:
            logger.warning(
                f"Cost {self.stats.instance_cost:.2f} exceeds limit {self.args.per_instance_cost_limit:.2f}"
            )
            raise CostLimitExceededError("Instance cost limit exceeded")
        return cost

    def query(self, history: list[dict[str, str]]) -> str:
        raise NotImplementedError("Use a subclass of BaseModel")


class OpenAIModel(BaseModel):
    MODELS = {
        "gpt-3.5-turbo-0125": {
            "max_context": 16_385,
            "cost_per_input_token": 5e-07,
            "cost_per_output_token": 1.5e-06,
        },
        "gpt-3.5-turbo-1106": {
            "max_context": 16_385,
            "cost_per_input_token": 1.5e-06,
            "cost_per_output_token": 2e-06,
        },
        "gpt-3.5-turbo-16k-0613": {
            "max_context": 16_385,
            "cost_per_input_token": 1.5e-06,
            "cost_per_output_token": 2e-06,
        },
        "gpt-4-32k-0613": {
            "max_context": 32_768,
            "cost_per_input_token": 6e-05,
            "cost_per_output_token": 0.00012,
        },
        "gpt-4-0613": {
            "max_context": 8_192,
            "cost_per_input_token": 3e-05,
            "cost_per_output_token": 6e-05,
        },
        "gpt-4-1106-preview": {
            "max_context": 128_000,
            "cost_per_input_token": 1e-05,
            "cost_per_output_token": 3e-05,
        },
        "gpt-4-0125-preview": {
            "max_context": 128_000,
            "cost_per_input_token": 1e-05,
            "cost_per_output_token": 3e-05,
        },
        "gpt-4-turbo-2024-04-09": {
            "max_context": 128_000,
            "cost_per_input_token": 1e-05,
            "cost_per_output_token": 3e-05,
        },
    }

    SHORTCUTS = {
        "gpt3": "gpt-3.5-turbo-1106",
        "gpt3-legacy": "gpt-3.5-turbo-16k-0613",
        "gpt4": "gpt-4-1106-preview",
        "gpt4-legacy": "gpt-4-0613",
        "gpt4-0125": "gpt-4-0125-preview",
        "gpt3-0125": "gpt-3.5-turbo-0125",
        "gpt4-turbo": "gpt-4-turbo-2024-04-09",
    }

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)

        # Set OpenAI key
        cfg = config.Config(os.path.join(os.getcwd(), "keys.cfg"))
        if self.args.model_name.startswith("azure"):
            self.api_model = cfg["AZURE_OPENAI_DEPLOYMENT"]
            self.client = AzureOpenAI(api_key=cfg["AZURE_OPENAI_API_KEY"], azure_endpoint=cfg["AZURE_OPENAI_ENDPOINT"], api_version=cfg.get("AZURE_OPENAI_API_VERSION", "2024-02-01"))
        else:
            api_base_url: Optional[str] = cfg.get("OPENAI_API_BASE_URL", None)
            self.client = OpenAI(api_key=cfg["OPENAI_API_KEY"], base_url=api_base_url)

    def history_to_messages(
        self, history: list[dict[str, str]], is_demonstration: bool = False
    ) -> Union[str, list[dict[str, str]]]:
        """
        Create `messages` by filtering out all keys except for role/content per `history` turn
        """
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
            return '\n'.join([entry["content"] for entry in history])
        # Return history components with just role, content fields
        return [
            {k: v for k, v in entry.items() if k in ["role", "content"]}
            for entry in history
        ]

    @retry(
        wait=wait_random_exponential(min=1, max=15),
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type((CostLimitExceededError, RuntimeError)),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query the OpenAI API with the given `history` and return the response.
        """
        try:
            # Perform OpenAI API call
            response = self.client.chat.completions.create(
                messages=self.history_to_messages(history),
                model=self.api_model,
                temperature=self.args.temperature,
                top_p=self.args.top_p,
            )
        except BadRequestError:
            raise CostLimitExceededError(f"Context window ({self.model_metadata['max_context']} tokens) exceeded")
        # Calculate + update costs, return response
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens
        self.update_stats(input_tokens, output_tokens)
        return response.choices[0].message.content


class AnthropicModel(BaseModel):
    MODELS = {
        "claude-instant": {
            "max_context": 100_000,
            "cost_per_input_token": 1.63e-06,
            "cost_per_output_token": 5.51e-06,
        },
        "claude-2.0": {
            "max_context": 100_000,
            "cost_per_input_token": 1.102e-05,
            "cost_per_output_token": 3.268e-05,
        },
        "claude-2.1": {
            "max_context": 100_000,
            "cost_per_input_token": 1.102e-05,
            "cost_per_output_token": 3.268e-05,
        },
        "claude-3-opus-20240229": {
            "max_context": 200_000,
            "max_tokens": 4096,  # Max tokens to generate for Claude 3 models
            "cost_per_input_token": 1.5e-05,
            "cost_per_output_token": 7.5e-05,
        },
        "claude-3-sonnet-20240229": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 3e-06,
            "cost_per_output_token": 1.5e-05,
        },
        "claude-3-haiku-20240307": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 2.5e-07,
            "cost_per_output_token": 1.25e-06,
        },
    }

    SHORTCUTS = {
        "claude-2": "claude-2.1",
        "claude-opus": "claude-3-opus-20240229",
        "claude-sonnet": "claude-3-sonnet-20240229",
        "claude-haiku": "claude-3-haiku-20240307",
    }

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)

        # Set Anthropic key
        cfg = config.Config(os.path.join(os.getcwd(), "keys.cfg"))
        self.api = Anthropic(api_key=cfg["ANTHROPIC_API_KEY"])

    def history_to_messages(
        self, history: list[dict[str, str]], is_demonstration: bool = False
    ) -> Union[str, list[dict[str, str]]]:
        """
        Create `prompt` by filtering out all keys except for role/content per `history` turn
        Reference: https://docs.anthropic.com/claude/reference/complete_post
        """
        return anthropic_history_to_messages(self, history, is_demonstration)

    @retry(
        wait=wait_random_exponential(min=1, max=15),
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type((CostLimitExceededError, RuntimeError)),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query the Anthropic API with the given `history` and return the response.
        """
        return anthropic_query(self, history)


class BedrockModel(BaseModel):
    MODELS = {
        "anthropic.claude-instant-v1": {
            "max_context": 100_000,
            "max_tokens_to_sample": 4096,
            "cost_per_input_token": 8e-07,
            "cost_per_output_token": 2.4e-06,
        },
        "anthropic.claude-v2": {
            "max_context": 100_000,
            "max_tokens_to_sample": 4096,
            "cost_per_input_token": 8e-06,
            "cost_per_output_token": 2.4e-05,
        },
        "anthropic.claude-v2:1": {
            "max_context": 100_000,
            "max_tokens": 4096,
            "cost_per_input_token": 8e-06,
            "cost_per_output_token": 2.4e-05,
        },
        "anthropic.claude-3-opus-20240229-v1:0": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 1.5e-05,
            "cost_per_output_token": 7.5e-05,
        },
        "anthropic.claude-3-sonnet-20240229-v1:0": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 3e-06,
            "cost_per_output_token": 1.5e-05,
        },
        "anthropic.claude-3-haiku-20240307-v1:0": {
            "max_context": 200_000,
            "max_tokens": 4096,
            "cost_per_input_token": 2.5e-07,
            "cost_per_output_token": 1.25e-06,
        },
    }

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)

        # Extract provider from model ID
        # https://docs.aws.amazon.com/bedrock/latest/userguide/model-ids.html
        self.model_provider = self.api_model.split('.')[0]
        if self.model_provider == "anthropic":
            # Note: this assumes AWS credentials are already configured.
            # https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html
            self.api = AnthropicBedrock()
        elif self.model_provider in ["ai21", "amazon", "cohere", "meta", "mistral"]:
            raise NotImplementedError(f"{self.api_model} is not supported!")
        else:
            raise ValueError(f"Provider {self.model_provider} is not supported by Amazon Bedrock!")

    def history_to_messages(
        self, history: list[dict[str, str]], is_demonstration: bool = False
    ) -> Union[str, list[dict[str, str]]]:
        """
        Create `prompt` from the history of messages
        """
        if self.model_provider == "anthropic":
            return anthropic_history_to_messages(self, history, is_demonstration)
        else:
            raise NotImplementedError(f"{self.api_model} is not supported!")

    @retry(
        wait=wait_random_exponential(min=1, max=15),
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type((CostLimitExceededError, RuntimeError)),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query Amazon Bedrock with the given `history` and return the response.
        """
        if self.model_provider == "anthropic":
            return anthropic_query(self, history)
        else:
            raise NotImplementedError(f"{self.api_model} is not supported!")


def anthropic_history_to_messages(
        model: Union[AnthropicModel, BedrockModel], history: list[dict[str, str]], is_demonstration: bool = False
    ) -> Union[str, list[dict[str, str]]]:
    """
    Create `prompt` by filtering out all keys except for role/content per `history` turn
    Reference: https://docs.anthropic.com/claude/reference/complete_post
    """
    # Preserve behavior for older models
    if model.api_model in ["claude-instant", "claude-2.0"] or \
       (isinstance(model, BedrockModel) and model.api_model in ["anthropic.claude-instant-v1", "anthropic.claude-v2"]):
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
        # Map history to Claude format
        prompt = "\n\n"
        for entry in history:
            if entry["role"] in {"user", "system"}:
                prompt += f'{HUMAN_PROMPT} {entry["content"]}\n\n'
            elif entry["role"] == "assistant":
                prompt += f'{AI_PROMPT} {entry["content"]}\n\n'
        prompt += AI_PROMPT
        return prompt

    # Remove system messages if it is a demonstration
    if is_demonstration:
        history = [entry for entry in history if entry["role"] != "system"]
        return '\n'.join([entry["content"] for entry in history])

    # Return history components with just role, content fields (no system message)
    messages = [
        {
            k: v for k, v in entry.items()
            if k in ["role", "content"]
        }
        for entry in history if entry["role"] != "system"
    ]
    compiled_messages = []  # Combine messages from the same role
    last_role = None
    for message in reversed(messages):
        if last_role == message["role"]:
            compiled_messages[-1]["content"] = message["content"] + "\n" + compiled_messages[-1]["content"]
        else:
            compiled_messages.append(message)
        last_role = message["role"]
    compiled_messages = list(reversed(compiled_messages))
    # Replace any empty content values with a "(No output)"
    for message in compiled_messages:
        if message["content"].strip() == "":
            message["content"] = "(No output)"
    return compiled_messages


def anthropic_query(model: Union[AnthropicModel, BedrockModel], history: list[dict[str, str]]) -> str:
    """
    Query the Anthropic API with the given `history` and return the response.
    """
    # Preserve behavior for older models
    if model.api_model in ["claude-instant", "claude-2.0", "claude-2.1"] or \
       (isinstance(model, BedrockModel) and model.api_model in ["anthropic.claude-instant-v1", "anthropic.claude-v2"]):
        # Perform Anthropic API call
        prompt = anthropic_history_to_messages(model, history)
        if isinstance(model, BedrockModel):
            # Use a dummy Anthropic client since count_tokens
            # is not available in AnthropicBedrock
            # https://github.com/anthropics/anthropic-sdk-python/issues/353
            input_tokens = Anthropic().count_tokens(prompt)
        else:
            input_tokens = model.api.count_tokens(prompt)
        completion = model.api.completions.create(
            model=model.api_model,
            prompt=prompt,
            max_tokens_to_sample=model.model_metadata["max_context"] - input_tokens if isinstance(model, Anthropic) else model.model_metadata["max_tokens_to_sample"],
            temperature=model.args.temperature,
            top_p=model.args.top_p,
        )
        # Calculate + update costs, return response
        response = completion.completion
        if isinstance(model, BedrockModel):
            output_tokens = Anthropic().count_tokens(response)
        else:
            output_tokens = model.api.count_tokens(response)
        model.update_stats(input_tokens, output_tokens)
        return response

    # Get system message(s)
    system_message = "\n".join([
        entry["content"] for entry in history if entry["role"] == "system"
    ])
    messages = anthropic_history_to_messages(model, history)

    # Perform Anthropic API call
    response = model.api.messages.create(
        messages=messages,
        max_tokens=model.model_metadata["max_tokens"],
        model=model.api_model,
        temperature=model.args.temperature,
        top_p=model.args.top_p,
        system=system_message,
    )

    # Calculate + update costs, return response
    model.update_stats(
        response.usage.input_tokens,
        response.usage.output_tokens
    )
    response = "\n".join([x.text for x in response.content])
    return response


class OllamaModel(BaseModel):
    MODELS = defaultdict(lambda: {
        "max_context": 128_000,
        "cost_per_input_token": 0,
        "cost_per_output_token": 0,
    })

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)
        from ollama import Client
        self.client = Client(host=args.host_url)

    def history_to_messages(
        self, history: list[dict[str, str]], is_demonstration: bool = False
    ) -> Union[str, list[dict[str, str]]]:
        """
        Create `messages` by filtering out all keys except for role/content per `history` turn
        """
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
            return '\n'.join([entry["content"] for entry in history])
        # Return history components with just role, content fields
        return [
            {k: v for k, v in entry.items() if k in ["role", "content"]}
            for entry in history
        ]

    @retry(
        wait=wait_random_exponential(min=1, max=15),
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type((CostLimitExceededError, RuntimeError)),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query the Ollama API with the given `history` and return the response.
        """
        response = self.client.chat(
            model=self.api_model,
            messages=self.history_to_messages(history),
            options={
                "temperature": self.args.temperature,
                "top_p": self.args.top_p,
            }
        )
        # Calculate + update costs, return response
        if "prompt_eval_count" in response:
            input_tokens = response["prompt_eval_count"]
        else:
            logger.warning(
                "Prompt eval count not found in response. Using 0. "
                "This might be because the prompt has been cached. "
                "See https://github.com/princeton-nlp/SWE-agent/issues/44 "
                "and https://github.com/ollama/ollama/issues/3427."
            )
            input_tokens = 0
        output_tokens = response["eval_count"]
        self.update_stats(input_tokens, output_tokens)
        return response["message"]["content"]


class TogetherModel(BaseModel):
    # Check https://docs.together.ai/docs/inference-models for model names, context
    # Check https://www.together.ai/pricing for pricing
    MODELS = {
        "meta-llama/Llama-2-13b-chat-hf": {
            "max_context": 4096,
            "cost_per_input_token": 2.25e-07,
            "cost_per_output_token": 2.25e-07,
        },
        "meta-llama/Llama-2-70b-chat-hf": {
            "max_context": 4096,
            "cost_per_input_token": 9e-07,
            "cost_per_output_token": 9e-07,
        },
        "mistralai/Mistral-7B-Instruct-v0.2": {
            "max_context": 32768,
            "cost_per_input_token": 2e-07,
            "cost_per_output_token": 2e-07,
        },
        "togethercomputer/RedPajama-INCITE-7B-Chat": {
            "max_context": 2048,
            "cost_per_input_token": 2e-07,
            "cost_per_output_token": 2e-07,
        },
        "mistralai/Mixtral-8x7B-Instruct-v0.1": {
            "max_context": 32768,
            "cost_per_input_token": 6e-07,
            "cost_per_output_token": 6e-07,
        },
    }

    SHORTCUTS = {
        "llama13b": "meta-llama/Llama-2-13b-chat-hf",
        "llama70b": "meta-llama/Llama-2-70b-chat-hf",
        "mistral7b": "mistralai/Mistral-7B-Instruct-v0.2",
        "mixtral8x7b": "mistralai/Mixtral-8x7B-Instruct-v0.1",
        "redpajama7b": "togethercomputer/RedPajama-INCITE-7B-Chat",
    }

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)
        assert together.version >= '1.1.0', "Please upgrade to Together SDK v1.1.0 or later."

        # Set Together key
        cfg = config.Config(os.path.join(os.getcwd(), "keys.cfg"))
        together.api_key = cfg.TOGETHER_API_KEY

    def history_to_messages(
        self, history: list[dict[str, str]], is_demonstration: bool = False
    ) -> str:
        """
        Create `prompt` by filtering out all keys except for role/content per `history` turn
        """
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
        # Map history to TogetherAI format
        mapping = {"user": "human", "assistant": "bot", "system": "bot"}
        prompt = [f'<{mapping[d["role"]]}>: {d["content"]}' for d in history]
        prompt = "\n".join(prompt)
        prompt = f"{prompt}\n<bot>:"
        return prompt

    @retry(
        wait=wait_random_exponential(min=1, max=15),
        reraise=True,
        stop=stop_after_attempt(3),
        retry=retry_if_not_exception_type((CostLimitExceededError, RuntimeError)),
    )
    def query(self, history: list[dict[str, str]]) -> str:
        """
        Query the Together API with the given `history` and return the response.
        """
        # Perform Together API call
        prompt = self.history_to_messages(history)
        # Anthropic's count_tokens is convenient because it caches and utilizes huggingface/tokenizers, so we will use.
        max_tokens_to_sample = self.model_metadata["max_context"] - Anthropic().count_tokens(prompt)
        completion = together.Complete.create(
            model=self.api_model,
            prompt=prompt,
            max_tokens=max_tokens_to_sample,
            stop=["<human>"],
            temperature=self.args.temperature,
            top_p=self.args.top_p,
        )
        # Calculate + update costs, return response
        response = completion["choices"][0]["text"].split("<human>")[0]
        input_tokens = completion["usage"]["prompt_tokens"]
        output_tokens = completion["usage"]["completion_tokens"]
        self.update_stats(input_tokens, output_tokens)
        return response


class HumanModel(BaseModel):
    MODELS = {"human": {}}

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)

        # Determine which commands require multi-line input
        self.multi_line_command_endings = {
            command.name: command.end_name
            for command in commands
            if command.end_name is not None
        }

    def history_to_messages(
        self, history: list[dict[str, str]], is_demonstration: bool = False
    ) -> Union[str, list[dict[str, str]]]:
        """
        Create `messages` by filtering out all keys except for role/content per `history` turn
        """
        # Remove system messages if it is a demonstration
        if is_demonstration:
            history = [entry for entry in history if entry["role"] != "system"]
            return '\n'.join([entry["content"] for entry in history])
        # Return history components with just role, content fields
        return [
            {k: v for k, v in entry.items() if k in ["role", "content"]}
            for entry in history
        ]

    def query(self, history: list[dict[str, str]], action_prompt: str = "> ") -> str:
        """
        Logic for handling user input to pass to SWEEnv
        """
        action = input(action_prompt)
        command_name = action.split()[0] if action else ""

        # Special handling for multi-line input actions (i.e. edit)
        if command_name in self.multi_line_command_endings:
            buffer = [action]
            end_keyword = self.multi_line_command_endings[command_name]
            while True:
                action = input("... ")
                buffer.append(action)
                if action.rstrip() == end_keyword:
                    # Continue reading input until terminating keyword inputted
                    break
            action = "\n".join(buffer)
        elif action.strip() == "start_multiline_command":  # do arbitrary multi-line input
            buffer = []
            while True:
                action = input("... ")
                if action.rstrip() == "end_multiline_command":
                    break
                buffer.append(action)
            action = "\n".join(buffer)
        return action


class HumanThoughtModel(HumanModel):
    MODELS = {"human_thought": {}}

    def query(self, history: list[dict[str, str]]) -> str:
        """
        Logic for handling user input (both thought + action) to pass to SWEEnv
        """
        thought_all = ""
        thought = input("Thought (end w/ END_THOUGHT): ")
        while True:
            if "END_THOUGHT" in thought:
                thought = thought.split("END_THOUGHT")[0]
                thought_all += thought
                break
            thought_all += thought
            thought = input("... ")

        action = super().query(history, action_prompt="Action: ")

        return f"{thought_all}\n```\n{action}\n```"


class ReplayModel(BaseModel):
    MODELS = {"replay": {}}

    def __init__(self, args: ModelArguments, commands: list[Command]):
        super().__init__(args, commands)

        if self.args.replay_path is None or not os.path.exists(self.args.replay_path):
            raise ValueError(
                "--replay_path must point to a file that exists to run a replay policy"
            )

        self.replays = [
            list(json.loads(x).values())[0]
            for x in open(self.args.replay_path, "r").readlines()
        ]
        self.replay_idx = 0
        self.action_idx = 0

    def query(self, history: list[dict[str, str]]) -> str:
        """
        Logic for tracking which replay action to pass to SWEEnv
        """
        action = self.replays[self.replay_idx][self.action_idx]
        self.action_idx += 1

        # Assuming `submit` is always last action of replay trajectory
        if action == "submit":
            self.replay_idx += 1
            self.action_idx = 0

        return action


class InstantEmptySubmitTestModel(BaseModel):
    MODELS = {"instant_empty_submit": {}}

    def __init__(self, args: ModelArguments, commands: list[Command]):
        """This model immediately submits an empty reproduce.py. Useful for testing purposes"""
        super().__init__(args, commands)
        self._action_idx = 0

    def query(self, history: list[dict[str, str]]) -> str:
        # Need to at least do _something_ to submit
        if self._action_idx == 0:
            self._action_idx = 1
            action = "DISCUSSION\nblah blah\n\n```\ncreate reproduce.py\n```\n"
        elif self._action_idx == 1:
            self._action_idx = 0
            action = "DISCUSSION\nblargh glargh\n\n```\nsubmit\n```\n"
        return action



def get_model(args: ModelArguments, commands: Optional[list[Command]] = None):
    """
    Returns correct model object given arguments and commands
    """
    if commands is None:
        commands = []
    if args.model_name == "instant_empty_submit":
        return InstantEmptySubmitTestModel(args, commands)
    if args.model_name == "human":
        return HumanModel(args, commands)
    if args.model_name == "human_thought":
        return HumanThoughtModel(args, commands)
    if args.model_name == "replay":
        return ReplayModel(args, commands)
    elif args.model_name.startswith("gpt") or args.model_name.startswith("ft:gpt") or args.model_name.startswith("azure:gpt"):
        return OpenAIModel(args, commands)
    elif args.model_name.startswith("claude"):
        return AnthropicModel(args, commands)
    elif args.model_name.startswith("bedrock"):
        return BedrockModel(args, commands)
    elif args.model_name.startswith("ollama"):
        return OllamaModel(args, commands)
    elif args.model_name in TogetherModel.SHORTCUTS:
        return TogetherModel(args, commands)
    else:
        raise ValueError(f"Invalid model name: {args.model_name}")
