# InferGrid Progress

## fix/validate branch (2026-04-15)

### Completed

1. **All 81 unit tests pass** (were previously never executed)
   - Fixed test infrastructure: converted deprecated `event_loop` fixture + `run_until_complete()` pattern to native `async def` tests (compatible with `asyncio_mode = "auto"`)
   - Removed unused imports from test files

2. **Fixed TOCTOU race in TenantManager** (`src/infergrid/tenant/manager.py`)
   - Old code checked `self._semaphore._value > 0` then called `await self._semaphore.acquire()` separately, allowing a race between check and acquire
   - Fix: moved both the semaphore value check and the acquire under the same `_lock`, eliminating the race window

3. **Fixed deprecated asyncio API** (`src/infergrid/router/router.py`, both adapters)
   - `asyncio.get_event_loop().create_future()` replaced with `asyncio.get_running_loop().create_future()` in `PendingRequest`
   - `asyncio.get_event_loop().time()` replaced with `asyncio.get_running_loop().time()` in both `VLLMAdapter.start()` and `SGLangAdapter.start()`

4. **Fixed streaming session leak** (both `vllm_adapter/adapter.py` and `sglang_adapter/adapter.py`)
   - Old code created a `ClientSession` in `forward_request()` and passed it to `_stream_response()` as an async generator. If the caller abandoned iteration, the generator's `finally` block never fired and the session leaked
   - Fix: `_stream_response()` now creates and owns its own session via `async with`, guaranteeing cleanup. Non-streaming path also uses `async with` for the session

5. **Added port availability check** (`src/infergrid/router/router.py`)
   - `_allocate_port()` now verifies the port is actually free via a socket bind check before returning it
   - Added `_is_port_available()` static method

6. **Populated empty summary.md** (`results/results_llama31-8b_20260416_120938/summary.md`)
   - Replaced "No data" tables with actual data from the vLLM JSON profiling results
   - Added cross-GPU comparison data (A100 SXM, H100 SXM) from follow-up runs

7. **Corrected false claims in README.md**
   - Removed "81.6% compound loss" claim -- GPU util is >95%, waste is in scheduling quality
   - Removed implied 50-70% TTFT reduction claim -- reframed to admission control (2-4x p99 improvement)
   - Changed "KV cache tiering across GPU HBM, CPU RAM, and NVMe SSD" to "KV cache lifecycle tracking with planned LMCache integration"
   - Updated Phase 1 Results with actual cross-engine comparison data
   - Changed KV Tiering column from "Yes" to "Planned" in comparison table

8. **Corrected false claims in docs/phase1_findings.md**
   - Updated from A100 PCIe-only to include A100 SXM and H100 SXM cross-engine data
   - Added Finding 1b with cross-engine comparison showing <5% throughput gap
   - Added TTFT saturation data showing SGLang 2.2x better at c=256
   - Removed "50% TTFT reduction" benchmark target -- replaced with "2-4x p99 TTFT improvement through admission control"
   - Updated Finding 4 to note that cross-engine comparison is now complete

### Decisions Affecting Other Agents
- The TenantManager `try_acquire()` API is unchanged but the internal locking is different -- both the semaphore check and acquire now happen under `_lock`
- The `_stream_response()` method signature changed in both adapters (no longer takes a session parameter) -- any code calling it directly needs updating
- Port allocation may return non-consecutive ports if some are in use
- All claims in README and docs are now data-backed -- other agents should not re-introduce unsupported claims
