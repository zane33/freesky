"""
Stream health monitoring and intelligent failover system
"""

import asyncio
import time
import logging
from typing import Dict, List, Optional, Tuple
from collections import defaultdict, deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class StreamMetrics:
    """Stream performance metrics"""
    channel_id: str
    success_rate: float
    avg_response_time: float
    error_count: int
    last_success: float
    consecutive_failures: int
    quality_score: float

class StreamMonitor:
    """Monitor stream health and performance"""
    
    def __init__(self):
        self.metrics: Dict[str, StreamMetrics] = {}
        self.response_times: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10))
        self.error_counts: Dict[str, int] = defaultdict(int)
        self.success_counts: Dict[str, int] = defaultdict(int)
        self.last_checks: Dict[str, float] = {}
        # When each channel last FAILED, and how many failures it has strung
        # together since its last success. Both are needed because last_checks
        # only ever records successes: a channel that has never once resolved has
        # last_success == 0, which made should_skip_channel's backoff compare
        # against the epoch and therefore never skip anything.
        self.last_failures: Dict[str, float] = {}
        self.consecutive_failures: Dict[str, int] = defaultdict(int)

        # Performance thresholds
        self.max_response_time = 5.0  # seconds
        self.min_success_rate = 0.7   # 70%
        self.max_consecutive_failures = 3
        
    def record_stream_attempt(self, channel_id: str, success: bool, response_time: float = 0.0):
        """Record a stream attempt result"""
        current_time = time.time()
        
        if success:
            self.success_counts[channel_id] += 1
            self.response_times[channel_id].append(response_time)
            self.last_checks[channel_id] = current_time
            self.consecutive_failures[channel_id] = 0
        else:
            self.error_counts[channel_id] += 1
            self.last_failures[channel_id] = current_time
            self.consecutive_failures[channel_id] += 1
        
        # Update metrics
        self._update_metrics(channel_id)
        
    def _update_metrics(self, channel_id: str):
        """Update stream metrics for a channel"""
        total_attempts = self.success_counts[channel_id] + self.error_counts[channel_id]
        
        if total_attempts == 0:
            return
            
        success_rate = self.success_counts[channel_id] / total_attempts
        
        # Calculate average response time
        response_times = list(self.response_times[channel_id])
        avg_response_time = sum(response_times) / len(response_times) if response_times else 0.0
        
        # Consecutive failures, counted directly. This used to be inferred as
        # "total errors, if the last success was over 60s ago", which reported a
        # channel that had failed once an hour ago as freshly failing, and could
        # never exceed max_consecutive_failures — so the backoff in
        # should_skip_channel had no headroom to grow.
        consecutive_failures = self.consecutive_failures[channel_id]
        
        # Calculate quality score (0-1)
        quality_score = self._calculate_quality_score(success_rate, avg_response_time, consecutive_failures)
        
        self.metrics[channel_id] = StreamMetrics(
            channel_id=channel_id,
            success_rate=success_rate,
            avg_response_time=avg_response_time,
            error_count=self.error_counts[channel_id],
            last_success=self.last_checks.get(channel_id, 0),
            consecutive_failures=consecutive_failures,
            quality_score=quality_score
        )
        
    def _calculate_quality_score(self, success_rate: float, avg_response_time: float, consecutive_failures: int) -> float:
        """Calculate overall quality score for a stream"""
        # Base score from success rate (0-0.5)
        success_score = success_rate * 0.5
        
        # Response time score (0-0.3)
        if avg_response_time <= 1.0:
            response_score = 0.3
        elif avg_response_time <= 3.0:
            response_score = 0.2
        elif avg_response_time <= 5.0:
            response_score = 0.1
        else:
            response_score = 0.0
            
        # Failure penalty (0-0.2)
        failure_penalty = min(consecutive_failures * 0.05, 0.2)
        
        return max(0.0, success_score + response_score - failure_penalty)
    
    def is_stream_healthy(self, channel_id: str) -> bool:
        """Check if a stream is considered healthy"""
        if channel_id not in self.metrics:
            return True  # Unknown streams are considered healthy initially
            
        metrics = self.metrics[channel_id]
        
        return (
            metrics.success_rate >= self.min_success_rate and
            metrics.avg_response_time <= self.max_response_time and
            metrics.consecutive_failures < self.max_consecutive_failures
        )
    
    def get_stream_priority(self, channel_id: str) -> int:
        """Get stream priority (1=highest, 3=lowest)"""
        if channel_id not in self.metrics:
            return 2  # Medium priority for unknown streams
            
        quality_score = self.metrics[channel_id].quality_score
        
        if quality_score >= 0.8:
            return 1  # High priority
        elif quality_score >= 0.5:
            return 2  # Medium priority
        else:
            return 3  # Low priority
    
    def get_best_channels(self, channel_ids: List[str], limit: int = 5) -> List[str]:
        """Get best performing channels sorted by quality"""
        channel_scores = []
        
        for channel_id in channel_ids:
            if channel_id in self.metrics:
                score = self.metrics[channel_id].quality_score
            else:
                score = 0.5  # Default score for unknown channels
            channel_scores.append((channel_id, score))
        
        # Sort by score descending
        channel_scores.sort(key=lambda x: x[1], reverse=True)
        
        return [channel_id for channel_id, _ in channel_scores[:limit]]
    
    def should_skip_channel(self, channel_id: str) -> bool:
        """Whether to skip a channel that keeps failing, under exponential backoff.

        Args:
            channel_id: channel being considered.

        Returns:
            True while the channel is inside its backoff window.

        The backoff is measured from the last ATTEMPT, not the last success. It
        used to use `metrics.last_success`, which defaults to 0 for a channel that
        has never resolved — so `time.time() - 0` was ~1.8e9 seconds, always larger
        than any retry window, and the breaker never fired for exactly the channels
        it existed to protect. During the 2026-09 off-air incident that left 18
        permanently failing channels retrying at full cost forever.
        """
        if channel_id not in self.metrics:
            return False

        metrics = self.metrics[channel_id]

        # Skip if too many consecutive failures
        if metrics.consecutive_failures >= self.max_consecutive_failures:
            last_attempt = max(metrics.last_success, self.last_failures.get(channel_id, 0))
            if not last_attempt:
                return False  # nothing recorded yet; let it try
            time_since_last = time.time() - last_attempt
            min_retry_time = 60 * (2 ** min(metrics.consecutive_failures - self.max_consecutive_failures, 3))
            return time_since_last < min_retry_time

        return False
    
    def get_metrics_summary(self) -> Dict:
        """Get summary of all stream metrics"""
        total_channels = len(self.metrics)
        healthy_channels = sum(1 for channel_id in self.metrics if self.is_stream_healthy(channel_id))
        
        if total_channels == 0:
            return {"total_channels": 0, "healthy_channels": 0, "health_rate": 0.0}
            
        avg_quality = sum(m.quality_score for m in self.metrics.values()) / total_channels
        
        return {
            "total_channels": total_channels,
            "healthy_channels": healthy_channels,
            "health_rate": healthy_channels / total_channels,
            "avg_quality_score": round(avg_quality, 3),
            "metrics_by_channel": {
                channel_id: {
                    "success_rate": round(m.success_rate, 3),
                    "avg_response_time": round(m.avg_response_time, 2),
                    "quality_score": round(m.quality_score, 3),
                    "consecutive_failures": m.consecutive_failures,
                    "healthy": self.is_stream_healthy(channel_id)
                }
                for channel_id, m in self.metrics.items()
            }
        }
    
    def cleanup_old_metrics(self, max_age_hours: int = 24):
        """Clean up metrics for channels not accessed recently"""
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600
        
        channels_to_remove = []
        for channel_id, metrics in self.metrics.items():
            if current_time - metrics.last_success > max_age_seconds:
                channels_to_remove.append(channel_id)
        
        for channel_id in channels_to_remove:
            del self.metrics[channel_id]
            if channel_id in self.response_times:
                del self.response_times[channel_id]
            if channel_id in self.error_counts:
                del self.error_counts[channel_id]
            if channel_id in self.success_counts:
                del self.success_counts[channel_id]
            if channel_id in self.last_checks:
                del self.last_checks[channel_id]
                
        if channels_to_remove:
            logger.info(f"Cleaned up metrics for {len(channels_to_remove)} inactive channels")

# Global monitor instance
stream_monitor = StreamMonitor()