/* ==========================================
   Google Cloud QnA Dashboard: Minimal Client Logic
   Controls SSE stream handling, live rendering, & table-slot updates.
   ========================================== */

document.addEventListener("DOMContentLoaded", () => {
  const queryInput = document.getElementById("query-input");
  const btnSubmit = document.getElementById("btn-submit");
  const systemStatusMsg = document.getElementById("system-status-msg");
  const progressSpinner = document.getElementById("progress-spinner");
  const flowStatus = document.getElementById("flow-status");

  // Table Slots & Sections
  const synthesisText = document.getElementById("synthesis-text");
  const evaluationText = document.getElementById("evaluation-text");
  const finalReportContent = document.getElementById("final-report-content");
  const finalStatus = document.getElementById("final-status");

  let eventSource = null;
  let activePillarsGlobal = [];
  let pillarToSlotMap = {}; // Maps pillar name -> slot number (1, 2, or 3)
  
  let synthesisAccumulatedText = "";
  let evaluationAccumulatedText = "";
  let remediationAccumulatedText = "";

  // Initialize mermaid if loaded
  if (typeof mermaid !== "undefined") {
    mermaid.initialize({
      startOnLoad: false,
      theme: 'neutral',
      securityLevel: 'loose',
      flowchart: {
        useWidth: true,
        htmlLabels: true
      }
    });
  }

  const PILLAR_DISPLAY_NAMES = {
    "APIs_Applications": "APIs and Applications",
    "Application_Modernization": "Application Modernization",
    "Artificial_Intelligence": "Artificial Intelligence",
    "Data_Analytics": "Data Analytics",
    "Databases": "Databases",
    "Infrastructure": "Infrastructure",
    "Productivity_Collaboration": "Productivity and Collaboration",
    "Security": "Security"
  };

  // Preprocessor to clean up markdown, remove bold weights from URLs, and keep them normal
  function cleanMarkdownText(text) {
    if (!text) return "";
    
    // 1. Remove bold/italic formatting from URLs: **http://...** or *http://...* -> http://...
    let cleaned = text.replace(/[\*_*]{1,3}(https?:\/\/[^\s\)\*_*]+)[\*_*]{1,3}/g, "$1");
    
    // 2. Also ensure raw URLs in the text are not wrapped in bold/italic when they are inside a list
    cleaned = cleaned.replace(/^\s*\*\s+[\*_*]{1,2}(https?:\/\/[^\s]+)[\*_*]{1,2}/gm, "* $1");

    return cleaned;
  }

  function renderMarkdown(container, text, runMermaid = false) {
    if (!text) {
      container.innerHTML = "";
      return;
    }
    const cleaned = cleanMarkdownText(text);
    if (typeof marked !== "undefined") {
      container.innerHTML = marked.parse(cleaned);
    } else {
      container.textContent = text;
    }

    if (runMermaid) {
      // Find all code blocks of language-mermaid or similar and transform them to div.mermaid
      const codeBlocks = container.querySelectorAll("pre code.language-mermaid, pre.language-mermaid");
      if (codeBlocks.length > 0) {
        let hasMermaid = false;
        codeBlocks.forEach(block => {
          const pre = block.tagName === "PRE" ? block : block.parentElement;
          const codeText = block.textContent.trim();
          if (codeText) {
            const div = document.createElement("div");
            div.className = "mermaid";
            div.textContent = codeText;
            pre.parentNode.replaceChild(div, pre);
            hasMermaid = true;
          }
        });
        
        if (hasMermaid && typeof mermaid !== "undefined") {
          try {
            mermaid.run({
              nodes: container.querySelectorAll(".mermaid")
            }).catch(err => {
              console.error("Mermaid rendering failed async:", err);
            });
          } catch (e) {
            console.error("Mermaid run error sync:", e);
          }
        }
      }
    }
  }


  // Submit button event listener
  btnSubmit.addEventListener("click", () => {
    const query = queryInput.value.trim();
    if (!query) {
      alert("질문 내용을 입력해 주십시오.");
      return;
    }
    startOrchestration(query);
  });

  // Core Orchestration Driver via SSE
  function startOrchestration(query) {
    // 1. Reset UI State
    btnSubmit.disabled = true;
    progressSpinner.style.display = "inline-block";
    flowStatus.textContent = "RUNNING";
    flowStatus.className = "badge active";
    systemStatusMsg.textContent = "멀티 에이전트 시스템 자문 오케스트레이션을 초기화하는 중입니다...";

    // Reset Sub-agent slots
    pillarToSlotMap = {};
    for (let i = 1; i <= 3; i++) {
      const titleEl = document.querySelector(`#subagent-slot-${i} .cell-title`);
      const badgeEl = document.getElementById(`subagent-badge-${i}`);
      const contentEl = document.getElementById(`subagent-content-${i}`);
      
      titleEl.innerHTML = `<i class="fa-solid fa-gears"></i> 전문가 #${i} 답변`;
      badgeEl.textContent = "WAITING";
      badgeEl.className = "badge";
      contentEl.innerHTML = `<p class="placeholder-text">대기 중...</p>`;
    }
    
    // Reset output sections
    synthesisAccumulatedText = "";
    evaluationAccumulatedText = "";
    remediationAccumulatedText = "";

    synthesisText.innerHTML = `<p class="placeholder-text"><i class="fa-solid fa-circle-notch fa-spin"></i> 아키텍처 합성 초안 생성을 대기하는 중입니다...</p>`;
    evaluationText.innerHTML = `<p class="placeholder-text">사실 정합성 검증 피드백 대기 중입니다...</p>`;
    finalReportContent.innerHTML = `<p class="placeholder-text">최종 아키텍처 자문 보고서 생성을 대기 중입니다...</p>`;
    
    // Hide streaming badges initially
    document.getElementById("synthesis-streaming-badge").style.display = "none";
    document.getElementById("evaluation-streaming-badge").style.display = "none";

    finalStatus.textContent = "WAITING";
    finalStatus.className = "badge";

    // Reset Topology Nodes and Edges to Idle
    resetTopologyToIdle();

    // 2. Open Server-Sent Events (SSE) stream
    const encodedQuery = encodeURIComponent(query);
    const streamUrl = `/api/stream?query=${encodedQuery}`;
    
    if (eventSource) {
      eventSource.close();
    }
    
    eventSource = new EventSource(streamUrl);

    // Master node active on startup
    setNodeState("node-master", "active");

    // SSE Stream Handlers
    eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleStreamEvent(data);
      } catch (err) {
        console.error("Failed to parse SSE packet:", err);
      }
    };

    eventSource.onerror = (err) => {
      console.error("SSE Connection Error:", err);
      systemStatusMsg.textContent = "스트리밍 연결에 실패했거나 끊겼습니다. 백엔드 가동 상황을 검증하십시오.";
      flowStatus.textContent = "ERROR";
      flowStatus.className = "badge";
      progressSpinner.style.display = "none";
      btnSubmit.disabled = false;
      eventSource.close();
    };
  }

  // SSE Event Handler Router
  function handleStreamEvent(data) {
    const { event } = data;

    switch (event) {
      case "status":
        systemStatusMsg.textContent = data.message;
        break;

      case "phase_change":
        handlePhaseChange(data.phase, data.status);
        break;

      case "routing_done":
        activePillarsGlobal = data.pillars || [];
        setRoutingTopology(activePillarsGlobal);
        preRenderSlots(activePillarsGlobal);
        break;

      case "subagent_done":
        renderSubAgentSlot(data.pillar, data.success, data.content);
        break;

      case "synthesis_chunk":
        if (!synthesisAccumulatedText) {
          synthesisText.innerHTML = "";
          const badge = document.getElementById("synthesis-streaming-badge");
          if (badge) {
            badge.style.display = "inline-block";
            badge.className = "badge active";
          }
        }
        synthesisAccumulatedText += data.text;
        renderMarkdown(synthesisText, synthesisAccumulatedText, false);
        synthesisText.scrollTop = synthesisText.scrollHeight; // Auto-scroll
        break;

      case "evaluation_chunk":
        if (!evaluationAccumulatedText) {
          evaluationText.innerHTML = "";
          const badge = document.getElementById("evaluation-streaming-badge");
          if (badge) {
            badge.style.display = "inline-block";
            badge.className = "badge active";
          }
        }
        evaluationAccumulatedText += data.text;
        renderMarkdown(evaluationText, evaluationAccumulatedText, false);
        evaluationText.scrollTop = evaluationText.scrollHeight; // Auto-scroll
        break;

      case "remediation_chunk":
        if (!remediationAccumulatedText) {
          finalReportContent.innerHTML = "";
          finalStatus.textContent = "GENERATING";
          finalStatus.className = "badge active";
        }
        remediationAccumulatedText += data.text;
        renderMarkdown(finalReportContent, remediationAccumulatedText, false);
        finalReportContent.scrollTop = finalReportContent.scrollHeight; // Auto-scroll
        break;

      case "final_report":
        finalStatus.textContent = "VERIFIED";
        finalStatus.className = "badge";
        remediationAccumulatedText = data.report;
        renderMarkdown(finalReportContent, data.report, true);
        break;

      case "done":
        finishOrchestration();
        break;

      case "error":
        handleErrorEvent(data.message);
        break;

      default:
        console.warn("Unknown SSE Event:", event);
    }
  }

  // Handle high-level milestones transition
  function handlePhaseChange(phase, status) {
    if (status === "active") {
      switch (phase) {
        case "routing":
          setNodeState("node-master", "completed");
          setNodeState("node-router", "active");
          setEdgeState("edge-master-router", "active");
          break;

        case "subagents":
          setNodeState("node-router", "completed");
          setEdgeState("edge-master-router", "completed");
          // Activate subagent nodes
          activePillarsGlobal.forEach(p => {
            setNodeState(`node-${p}`, "active");
            setEdgeState(`edge-router-${p}`, "active");
          });
          break;

        case "synthesis":
          activePillarsGlobal.forEach(p => {
            setNodeState(`node-${p}`, "completed");
            setEdgeState(`edge-router-${p}`, "completed");
            setEdgeState(`edge-${p}-synthesizer`, "active");
          });
          setNodeState("node-synthesizer", "active");
          break;

        case "evaluation":
          const sBadge = document.getElementById("synthesis-streaming-badge");
          if (sBadge) sBadge.style.display = "none";

          setNodeState("node-synthesizer", "completed");
          activePillarsGlobal.forEach(p => {
            setEdgeState(`edge-${p}-synthesizer`, "completed");
          });
          setEdgeState("edge-synthesizer-evaluator", "active");
          setNodeState("node-evaluator", "active");
          break;

        case "remediation":
          const eBadge = document.getElementById("evaluation-streaming-badge");
          if (eBadge) eBadge.style.display = "none";

          setNodeState("node-evaluator", "completed");
          setEdgeState("edge-synthesizer-evaluator", "completed");
          setEdgeState("edge-evaluator-remediator", "active");
          setNodeState("node-remediator", "active");
          break;
      }
    } else if (status === "completed") {
      switch (phase) {
        case "remediation":
          setNodeState("node-remediator", "completed");
          setEdgeState("edge-evaluator-remediator", "completed");
          break;
      }
    }
  }

  // Routing complete UI controller for Topology
  function setRoutingTopology(activePillars) {
    const allPillars = [
      "APIs_Applications", "Application_Modernization", "Artificial_Intelligence",
      "Data_Analytics", "Databases", "Infrastructure", "Productivity_Collaboration", "Security"
    ];

    allPillars.forEach(p => {
      const node = document.getElementById(`node-${p}`);
      const edgeIn = document.getElementById(`edge-router-${p}`);
      const edgeOut = document.getElementById(`edge-${p}-synthesizer`);

      if (activePillars.includes(p)) {
        if (node) {
          node.classList.remove("idle", "inactive");
          node.style.opacity = "1.0";
        }
      } else {
        if (node) {
          node.classList.add("inactive");
          node.style.opacity = "0.1";
        }
        if (edgeIn) edgeIn.style.opacity = "0.05";
        if (edgeOut) edgeOut.style.opacity = "0.05";
      }
    });
  }

  // Pre-register active pillars to slots 1, 2, and 3
  function preRenderSlots(pillars) {
    pillarToSlotMap = {};
    
    pillars.forEach((p, index) => {
      if (index < 3) {
        const slotNum = index + 1;
        pillarToSlotMap[p] = slotNum;

        const titleEl = document.querySelector(`#subagent-slot-${slotNum} .cell-title`);
        const badgeEl = document.getElementById(`subagent-badge-${slotNum}`);
        const contentEl = document.getElementById(`subagent-content-${slotNum}`);
        
        const formattedName = PILLAR_DISPLAY_NAMES[p] || p;
        titleEl.innerHTML = `<i class="${getIconClass(p)}"></i> ${formattedName} 에이전트`;
        badgeEl.textContent = "RUNNING";
        badgeEl.className = "badge active";
        contentEl.innerHTML = `<p class="placeholder-text"><i class="fa-solid fa-circle-notch fa-spin"></i> 분석 개시 중...</p>`;
      }
    });

    // Unused slots display "NOT ROUTED"
    for (let i = pillars.length; i < 3; i++) {
      const slotNum = i + 1;
      const titleEl = document.querySelector(`#subagent-slot-${slotNum} .cell-title`);
      const badgeEl = document.getElementById(`subagent-badge-${slotNum}`);
      const contentEl = document.getElementById(`subagent-content-${slotNum}`);

      titleEl.innerHTML = `<i class="fa-solid fa-gears"></i> 전문가 #${slotNum} 답변`;
      badgeEl.textContent = "NOT ACTIVE";
      badgeEl.className = "badge gray";
      contentEl.innerHTML = `<p class="placeholder-text" style="color: #999;">이번 분석 과정에는 라우팅되지 않았습니다.</p>`;
    }
  }

  // Bind response of subagent to its designated slot
  function renderSubAgentSlot(pillar, success, content) {
    // 1. Update SVG node state
    setNodeState(`node-${pillar}`, "completed");
    setEdgeState(`edge-router-${pillar}`, "completed");

    // 2. Find designated slot index
    const slotNum = pillarToSlotMap[pillar];
    if (!slotNum) return; // Defense check

    const badgeEl = document.getElementById(`subagent-badge-${slotNum}`);
    const contentEl = document.getElementById(`subagent-content-${slotNum}`);

    if (badgeEl) {
      badgeEl.textContent = success ? "DONE" : "ERROR";
      badgeEl.className = success ? "badge gray" : "badge active";
    }

    if (contentEl) {
      if (success) {
        renderMarkdown(contentEl, content, false);
      } else {
        contentEl.innerHTML = `<p class="placeholder-text" style="color: #aa0000;">분석 실패: ${content}</p>`;
      }
    }
  }

  // Gracefully wrap up orchestration
  function finishOrchestration() {
    progressSpinner.style.display = "none";
    flowStatus.textContent = "COMPLETED";
    flowStatus.className = "badge";
    btnSubmit.disabled = false;
    
    // Hide all streaming badges
    const sBadge = document.getElementById("synthesis-streaming-badge");
    if (sBadge) sBadge.style.display = "none";
    const eBadge = document.getElementById("evaluation-streaming-badge");
    if (eBadge) eBadge.style.display = "none";

    // Ensure all target nodes are complete
    setNodeState("node-master", "completed");
    setNodeState("node-router", "completed");
    setNodeState("node-synthesizer", "completed");
    setNodeState("node-evaluator", "completed");
    setNodeState("node-remediator", "completed");

    activePillarsGlobal.forEach(p => {
      setNodeState(`node-${p}`, "completed");
      setEdgeState(`edge-router-${p}`, "completed");
      setEdgeState(`edge-${p}-synthesizer`, "completed");
    });

    // Render final content with Mermaid rendering enabled
    renderMarkdown(finalReportContent, remediationAccumulatedText || finalReportContent.textContent, true);

    if (eventSource) {
      eventSource.close();
    }
  }

  // Handle runtime stream errors
  function handleErrorEvent(msg) {
    alert(`오케스트레이션 에러: ${msg}`);
    systemStatusMsg.textContent = `에러 발생: ${msg}`;
    flowStatus.textContent = "ERROR";
    flowStatus.className = "badge";
    progressSpinner.style.display = "none";
    btnSubmit.disabled = false;

    // Hide all streaming badges
    const sBadge = document.getElementById("synthesis-streaming-badge");
    if (sBadge) sBadge.style.display = "none";
    const eBadge = document.getElementById("evaluation-streaming-badge");
    if (eBadge) eBadge.style.display = "none";

    if (eventSource) {
      eventSource.close();
    }
  }

  // ==========================================
  // SVG Topology Manipulation Utilities
  // ==========================================

  function setNodeState(nodeId, state) {
    const node = document.getElementById(nodeId);
    if (!node) return;

    node.classList.remove("idle", "active", "completed", "inactive");
    
    if (state === "active") {
      node.classList.add("active");
      node.style.opacity = "1";
    } else if (state === "completed") {
      node.classList.add("completed");
      node.style.opacity = "1";
    } else if (state === "idle") {
      node.classList.add("idle");
      node.style.opacity = "0.45";
    }
  }

  function setEdgeState(edgeId, state) {
    const edge = document.getElementById(edgeId);
    if (!edge) return;

    edge.classList.remove("active", "completed");

    if (state === "active") {
      edge.classList.add("active");
      edge.style.opacity = "1";
    } else if (state === "completed") {
      edge.classList.add("completed");
      edge.style.opacity = "0.7";
    } else {
      edge.style.opacity = "0.08";
    }
  }

  function resetTopologyToIdle() {
    const allNodes = [
      "node-master", "node-router", 
      "node-APIs_Applications", "node-Application_Modernization", "node-Artificial_Intelligence",
      "node-Data_Analytics", "node-Databases", "node-Infrastructure", "node-Productivity_Collaboration", "node-Security",
      "node-synthesizer", "node-evaluator", "node-remediator"
    ];

    allNodes.forEach(nodeId => setNodeState(nodeId, "idle"));

    const allEdges = [
      "edge-master-router", 
      "edge-router-APIs_Applications", "edge-router-Application_Modernization", "edge-router-Artificial_Intelligence",
      "edge-router-Data_Analytics", "edge-router-Databases", "edge-router-Infrastructure", "edge-router-Productivity_Collaboration", "edge-router-Security",
      "edge-APIs_Applications-synthesizer", "edge-Application_Modernization-synthesizer", "edge-Artificial_Intelligence-synthesizer",
      "edge-Data_Analytics-synthesizer", "edge-Databases-synthesizer", "edge-Infrastructure-synthesizer", "edge-Productivity_Collaboration-synthesizer", "edge-Security-synthesizer",
      "edge-synthesizer-evaluator", "edge-evaluator-remediator"
    ];

    allEdges.forEach(edgeId => {
      const edge = document.getElementById(edgeId);
      if (edge) {
        edge.classList.remove("active", "completed");
        edge.style.opacity = "0.08";
      }
    });
  }

  function getIconClass(pillar) {
    switch (pillar) {
      case "APIs_Applications": return "fa-solid fa-code";
      case "Application_Modernization": return "fa-solid fa-cubes";
      case "Artificial_Intelligence": return "fa-solid fa-robot";
      case "Data_Analytics": return "fa-solid fa-chart-line";
      case "Databases": return "fa-solid fa-database";
      case "Infrastructure": return "fa-solid fa-server";
      case "Productivity_Collaboration": return "fa-solid fa-users";
      case "Security": return "fa-solid fa-shield-halved";
      default: return "fa-solid fa-brain";
    }
  }
});
