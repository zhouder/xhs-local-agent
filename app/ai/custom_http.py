from app.ai.openai_compatible import AIProviderError, OpenAICompatibleProvider


class CustomHttpProvider(OpenAICompatibleProvider):
    provider_name = "custom_http"

    def _raw_completion(self, system: str, user: str) -> str:
        raise AIProviderError("custom_http profile is saved, but its request template adapter is not implemented in this MVP")
