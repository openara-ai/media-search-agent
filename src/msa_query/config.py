from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    # Vector collection names
    vector_collection_image: str = Field(default="image_emb", description="Name of image embeddings collection")
    vector_collection_video: str = Field(default="video_emb", description="Name of video keyframe embeddings collection")
    vector_collection_caption: str = Field(default="caption_emb", description="Name of caption embeddings collection")
    vector_collection_asr: str = Field(default="asr_emb", description="Name of ASR embeddings collection")
    
    # Database settings
    db_url: str = Field(default="sqlite:///./index/media.sqlite", description="SQLite database URL")
    
    # Search parameters
    retrieval_top_k: int = Field(default=200, description="Number of candidates to retrieve")
    rerank_top_k: int = Field(default=50, description="Number of results after reranking")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )

settings = Settings()