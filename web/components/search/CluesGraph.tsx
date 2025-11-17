import React, { useRef, useEffect, useMemo, useState, useCallback } from "react";
import dynamic from "next/dynamic";
import { Loader2, ChevronDown, ChevronUp, LayoutGrid } from "lucide-react";
import type { RelationGraphExpose } from "relation-graph-react";
import type { Clue, GraphData, GraphNode, GraphLine, Node } from "@/types/search-response";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { StageFilter } from "./StageFilter";
import { GraphStats } from "./GraphStats";
import { NodeDetail } from "./NodeDetail";
import { filterCluesByDisplayMode } from "@/lib/clue-path-utils";  // 🆕 导入路径反推工具

// 动态导入 RelationGraph，禁用 SSR
const RelationGraph = dynamic(() => import("relation-graph-react"), {
  ssr: false,
  loading: () => (
    <div className="flex items-center justify-center h-full">
      <Loader2 className="w-8 h-8 animate-spin text-muted-foreground" />
    </div>
  ),
});

interface CluesGraphProps {
  /** 线索数据 */
  clues: Clue[];
  /** 容器高度 */
  height?: number;
}

/**
 * Clues 知识图谱可视化组件
 *
 * 使用 relation-graph-react 展示搜索推理链路：
 * - query → entity → event → section
 * - 支持阶段过滤（recall/expand/rerank）
 * - 节点/连线点击交互
 */
export function CluesGraph({ clues, height = 600 }: CluesGraphProps) {
  const graphRef = useRef<RelationGraphExpose>(null!);

  // 阶段过滤状态
  const [selectedStages, setSelectedStages] = useState<string[]>([
    "prepare",
    "recall",
    "expand",
    "rerank",
  ]);

  // 布局模式 - 默认力导向
  const [layoutMode, setLayoutMode] = useState<'force' | 'tree'>('force');

  // 选中的节点（用于显示详情）
  const [selectedNode, setSelectedNode] = useState<Node | null>(null);

  // 图例展开状态（维度/分类/原文）
  const [isLegendExpanded, setIsLegendExpanded] = useState(false);

  // 控制栏折叠状态（阶段/类型/统计）
  const [isControlBarCollapsed, setIsControlBarCollapsed] = useState(true);

  // 精简模式状态 - 默认启用精简模式
  const [isSimplifiedMode, setIsSimplifiedMode] = useState(true);

  // 图谱加载状态
  const [isGraphLoading, setIsGraphLoading] = useState(false);

  // 根据选中阶段过滤线索
  const filteredClues = useMemo(() => {
    return clues.filter((clue) => selectedStages.includes(clue.stage));
  }, [clues, selectedStages]);

  // 统计图例数据
  const legendStats = useMemo(() => {
    const types = new Set<string>();
    const categories = new Set<string>();
    const stages = new Set<string>();

    filteredClues.forEach((clue) => {
      types.add(clue.from.type);
      types.add(clue.to.type);
      if (clue.from.category && clue.from.category.trim()) {
        categories.add(clue.from.category);
      }
      if (clue.to.category && clue.to.category.trim()) {
        categories.add(clue.to.category);
      }
      stages.add(clue.stage);
    });

    // 类型按指定顺序排列
    const typeOrder = ['query', 'entity', 'event', 'section'];
    const sortedTypes = typeOrder.filter(t => types.has(t));
    Array.from(types).forEach(t => {
      if (!typeOrder.includes(t)) {
        sortedTypes.push(t);
      }
    });

    // 分类标签分类（修正后的逻辑）
    const allCategories = Array.from(categories);
    const entityCategories: string[] = [];  // 实体维度：action, location 等
    const eventCategories: string[] = [];   // 事项分类：产品、技术、技能等
    const sectionCategories: string[] = []; // 原文块：SQL-X 格式

    // 实体维度关键词
    const entityCategoryKeywords = ['action', 'location', 'origin', 'person', 'tags', 'time', 'topic'];

    // 事项分类关键词
    const eventCategoryKeywords = ['产品', '技术', '技能', '研究', '社交', '管理'];

    allCategories.forEach(cat => {
      if (/^SQL-\d+$/i.test(cat)) {
        // SQL-X 格式的是原文块
        sectionCategories.push(cat);
      } else if (entityCategoryKeywords.includes(cat)) {
        // 实体维度
        entityCategories.push(cat);
      } else if (eventCategoryKeywords.includes(cat)) {
        // 事项分类
        eventCategories.push(cat);
      }
    });

    return {
      types: sortedTypes,
      categories: allCategories.sort(),
      entityCategories: entityCategories.sort(),
      eventCategories: eventCategories.sort(),
      sectionCategories: sectionCategories.sort(),
      stages: Array.from(stages).sort(),
    };
  }, [filteredClues]);

  // 阶段样式配置
  const stageColors: Record<string, string> = {
    prepare: "#722ed1",
    recall: "#1890ff",
    expand: "#52c41a",
    rerank: "#fa8c16",
  };

  const stageWidths: Record<string, string> = {
    prepare: "2px",
    recall: "2.5px",
    expand: "2px",
    rerank: "3px",
  };

  // 转换为图谱数据
  const graphData = useMemo(() => {
    // 🆕 使用新的路径反推工具（带阶段判断）
    // 精简模式：根据 display_level="final" 的线索和选中的阶段组合反推完整路径
    // 全量模式：显示所有线索
    const clues = isSimplifiedMode
      ? filterCluesByDisplayMode(filteredClues, 'simplified', selectedStages)
      : filteredClues;

    const data = convertToGraphData(clues);
    console.log('Graph data:', data);
    console.log('Nodes count:', data.nodes.length);
    console.log('Lines count:', data.lines.length);
    console.log('Simplified mode:', isSimplifiedMode);
    console.log('Selected stages:', selectedStages);
    console.log('Display mode clues count:', clues.length);
    return data;
  }, [filteredClues, isSimplifiedMode, selectedStages]);

  // 初始化图谱 - 优化版
  const showGraph = useCallback(async () => {
    if (!graphRef.current) return;

    if (graphData.nodes.length === 0) {
      try {
        await graphRef.current.setJsonData({ nodes: [], lines: [] });
      } catch (error) {
        console.error('清空图谱失败:', error);
      }
      return;
    }

    try {
      // 🔑 关键优化：不显示加载遮罩，让力导向动画作为视觉反馈
      // setIsGraphLoading(true);

      // 🆕 先清空图谱，避免增量更新导致的重复线条问题
      await graphRef.current.setJsonData({ nodes: [], lines: [] });

      // 🆕 关键修复：力导向布局需要更长的重置时间
      // 50ms太短，引擎还没完全重置就加载新数据会导致跳过动画
      // 力导向需要200ms+让引擎完全重新初始化
      const resetDelay = layoutMode === 'force' ? 300 : 50;
      await new Promise(resolve => setTimeout(resolve, resetDelay));

      // 设置数据并在回调中处理初始化
      await graphRef.current.setJsonData(graphData, async (graphInstance) => {
        try {
          // 等待布局稳定
          // await new Promise(resolve => setTimeout(resolve, 100));

          // 先设置缩放为30%
          await graphInstance.setZoom(50);

          // 再居中显示
          await graphInstance.moveToCenter();
        } catch (error) {
          console.warn('图谱初始化调整失败:', error);
        }
        // 🔑 移除 finally 块，不需要关闭加载状态
      });
    } catch (error) {
      console.error('Error setting graph data:', error);
    }
  }, [graphData, layoutMode]);

  // 数据或布局变化时重新渲染图谱
  useEffect(() => {
      showGraph();
  }, [showGraph]);

  // 图谱配置
  const graphOptions = useMemo(() => {
    // 布局配置映射
    const layoutsConfig = {
      force: {
        label: '力导向布局',
        layoutName: 'force',
        layoutClassName: 'seeks-layout-force',
        force_node_repulsion: 1.5,
        force_line_elastic: 1.2,
        distance_coefficient: 1.3,
      },
      tree: {
        label: '树形布局',
        layoutName: 'tree',
        layoutClassName: 'seeks-layout-tree',
        from: 'top',           // 从上到下，层级更清晰
        min_per_width: 200,    // 增加横向间距
        min_per_height: 100,   // 增加纵向间距
      },
    };

    return {
      debug: false,

      // 布局配置 - 使用 layouts 数组
      layouts: [layoutsConfig[layoutMode]],

      // 节点样式
      defaultNodeShape: 0,  // 0=完全自定义，不使用默认形状
      defaultNodeWidth: 180,  // 统一宽度
      defaultNodeHeight: 85,  // 统一高度容纳正文块 + 标签
      defaultNodeColor: "transparent",  // 外层透明
      defaultNodeFontColor: "#ffffff",
      defaultNodeBorderWidth: 0,  // 无边框
      defaultNodeBorderColor: "transparent",
      defaultNodeFontSize: 13,

      // 启用 HTML 模板（完全自定义节点）
      defaultNodeUseHtml: true,  // 启用 HTML 渲染
      isHideNodeText: true,  // 隐藏默认文本，只显示 HTML

      // 连线样式 - 使用直线
      defaultLineShape: 1,
      defaultLineWidth: 2,
      defaultLineColor: "#90caf9",
      defaultLineFontSize: 9,      // 减小连线文字
      defaultJunctionPoint: 'border',  // 连接点在边界
      defaultLineTextPosition: 'start',  // 文字位置靠近起点
      defaultLineMarker: {
        markerWidth: 10,
        markerHeight: 10,
        refX: 8,
        refY: 5,
        data: 'M 0 0, V 10, L 10 5, Z',
      },

      // 交互设置
      allowShowMiniToolBar: true,  // 显示迷你工具栏
      allowShowMiniNameFilter: false,
      allowSwitchLineShape: true,  // 允许切换线条形状
      allowSwitchJunctionPoint: true,  // 允许切换连接点
      disableDragNode: false,
      moveToCenterWhenResize: true,

      // 缩放设置 - 默认30%缩放
      defaultZoom: 50,  // 默认 30%，显示更多节点
      disableZoom: false,
      zoomToFitWhenRefresh: false,  // 使用固定30%，不自动适配
      moveToCenterWhenRefresh: true,  // 刷新时自动居中
      wheelZoomDelta: 0.015,
      min_zoom: 20,
      max_zoom: 300
    } as any;  // 添加类型断言避免 RGOptions 类型冲突
  }, [layoutMode]);

  // 节点点击事件
  const handleNodeClick = (nodeObject: any, $event: any) => {
    const node = nodeObject.data as Node;
    setSelectedNode(node);
  };

  // 连线点击事件
  const handleLineClick = (lineObject: any, link: any, $event: any) => {
    const clue = lineObject.data as Clue;
    console.log("Clicked clue:", clue);
    // TODO: 显示线索详情
  };

  if (clues.length === 0) {
    return (
      <Card className="flex items-center justify-center h-96">
        <div className="text-center text-muted-foreground">
          <p className="text-lg">暂无线索数据</p>
          <p className="text-sm mt-2">请先执行搜索以查看知识图谱</p>
        </div>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {/* 整合的控制栏 + 图例 - macOS 风格 Card */}
      <Card className="border-gray-200/80 shadow-sm hover:shadow-md transition-shadow duration-200">
        <div className="px-4 py-3.5 space-y-3.5">
          {/* 第一行：阶段过滤 + 布局选择 */}
          <div className="flex items-center justify-between gap-4 flex-wrap">
            {/* 左侧：阶段过滤器 */}
            <StageFilter
              allStages={["prepare", "recall", "expand", "rerank"]}
              selectedStages={selectedStages}
              onStagesChange={setSelectedStages}
              clues={clues}
            />

            {/* 右侧：精简模式 + 布局切换器 + 展开/收起按钮 */}
            <div className="flex items-center gap-4">
              {/* 精简模式开关 */}
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-gray-700">精简</span>
                <Switch
                  checked={isSimplifiedMode}
                  onCheckedChange={setIsSimplifiedMode}
                  className="h-5"
                />
              </div>

              <Separator orientation="vertical" className="h-5 opacity-60" />

              {/* 布局选择器 */}
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-muted-foreground"></span>
                <Select value={layoutMode} onValueChange={(v) => setLayoutMode(v as 'force' | 'tree')}>
                  <SelectTrigger className="w-[100px] h-8 text-xs border-gray-200/80 hover:border-gray-300 transition-colors">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="force">力导向</SelectItem>
                    <SelectItem value="tree">树形</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <Separator orientation="vertical" className="h-5 opacity-60" />

              {/* 统一的展开/收起按钮 */}
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  const newCollapsed = !isControlBarCollapsed;
                  setIsControlBarCollapsed(newCollapsed);
                  // 展开时同时展开分类（如果有分类数据）
                  if (!newCollapsed && legendStats.categories.length > 0) {
                    setIsLegendExpanded(true);
                  } else {
                    setIsLegendExpanded(false);
                  }
                }}
                className="h-6 px-2 text-xs text-muted-foreground hover:text-foreground hover:bg-gray-100/80 transition-all duration-200 rounded-md"
              >
                {isControlBarCollapsed ? (
                  <>
                    <ChevronDown className="h-3 w-3 mr-1" />
                    展开
                  </>
                ) : (
                  <>
                    <ChevronUp className="h-3 w-3 mr-1" />
                    收起
                  </>
                )}
              </Button>
            </div>
          </div>

          {/* 第二行：阶段 - 类型 - 统计 - 可折叠 */}
          {!isControlBarCollapsed && (
            <div className="pt-2.5 border-t text-xs">
              <div className="flex items-center justify-between gap-6">
                {/* 左侧：阶段（包含 query 起始点） */}
                <div className="flex items-center gap-4">
                  <span className="font-semibold text-gray-700 w-[40px] text-left">阶段</span>
                  <div className="flex items-center gap-2">
                    {/* query 起始点 */}
                    <div className="flex items-center gap-1.5">
                      <div
                        className="w-2 h-2 rounded-full"
                        style={{ backgroundColor: "#1890ff" }}
                      />
                      <span className="font-medium text-gray-600">query</span>
                    </div>

                    {/* 其他阶段 */}
                    {legendStats.stages.map((stage) => (
                      <div key={stage} className="flex items-center gap-1.5">
                        <div
                          className="w-5 rounded-full"
                          style={{
                            backgroundColor: stageColors[stage] || "#90caf9",
                            height: stageWidths[stage] || "2px",
                          }}
                        />
                        <span className="font-medium text-gray-600">{stage}</span>
                      </div>
                    ))}
                  </div>
                </div>

                <Separator orientation="vertical" className="h-5 opacity-60" />

                {/* 类型 */}
                <div className="flex items-center gap-4">
                  <span className="font-semibold text-gray-700 w-[40px] text-left">类型</span>
                  <div className="flex items-center gap-2.5">
                    {legendStats.types.map((type) => (
                      <div key={type} className="flex items-center gap-1.5">
                        <div
                          className="w-3 h-3 rounded shadow-sm"
                          style={{ backgroundColor: getTypeColor(type) }}
                        />
                        <span className="font-medium text-gray-600">{type}</span>
                      </div>
                    ))}
                  </div>
                </div>

                <Separator orientation="vertical" className="h-5 opacity-60" />

                {/* 节点/连线统计 */}
                <GraphStats clues={filteredClues} />
              </div>
            </div>
          )}

          {/* 第三行：分类标签（可折叠，严格对齐） */}
          {!isControlBarCollapsed && isLegendExpanded && legendStats.categories.length > 0 && (
            <>
              <Separator className="opacity-60" />
              <div className="space-y-2.5 pb-6 text-xs">
                {/* 实体维度 */}
                {legendStats.entityCategories.length > 0 && (
                  <div className="flex items-start gap-4">
                    <span className="font-semibold text-gray-700 w-[50px] text-left pt-1">
                      维度
                    </span>
                    <div className="flex items-center gap-2 flex-wrap flex-1">
                      {legendStats.entityCategories.map((category) => (
                        <Badge
                          key={category}
                          variant="outline"
                          className="text-xs font-medium bg-green-50/80 border-green-200/60 text-green-700 hover:bg-green-100/80 transition-colors px-2.5 py-0.5 rounded-md"
                        >
                          {category}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}

                {/* 事项分类 */}
                {legendStats.eventCategories.length > 0 && (
                  <div className="flex items-start gap-4">
                    <span className="font-semibold text-gray-700 w-[50px] text-left-pt-1">
                      分类
                    </span>
                    <div className="flex items-center gap-2 flex-wrap flex-1">
                      {legendStats.eventCategories.map((category) => (
                        <Badge
                          key={category}
                          variant="outline"
                          className="text-xs font-medium bg-amber-50/80 border-amber-200/60 text-amber-700 hover:bg-amber-100/80 transition-colors px-2.5 py-0.5 rounded-md"
                        >
                          {category}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}

                {/* 原文块 */}
                {legendStats.sectionCategories.length > 0 && (
                  <div className="flex items-start gap-4">
                    <span className="font-semibold text-gray-700 w-[50px] text-left pt-1">
                      原文
                    </span>
                    <div className="flex items-center gap-2 flex-wrap flex-1">
                      {legendStats.sectionCategories.map((category) => (
                        <Badge
                          key={category}
                          variant="outline"
                          className="text-xs font-medium bg-blue-50/80 border-blue-200/60 text-blue-700 hover:bg-blue-100/80 transition-colors px-2.5 py-0.5 rounded-md"
                        >
                          {category}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </Card>

      {/* 图谱容器 - 优化圆角和阴影 */}
      <Card className="p-0 overflow-hidden border-gray-200/80 shadow-md hover:shadow-lg transition-shadow duration-200">
        <div style={{ width: '100%', height: `${height}px`, position: 'relative' }}>
          {/* 加载状态遮罩 */}
          {isGraphLoading && (
            <div className="absolute inset-0 flex items-center justify-center bg-white/80 z-10">
              <div className="flex flex-col items-center gap-3">
                <Loader2 className="w-8 h-8 animate-spin text-blue-600" />
                <span className="text-sm text-muted-foreground">图谱加载中...</span>
              </div>
            </div>
          )}
          <RelationGraph
            key={`graph-${layoutMode}`}
            ref={graphRef}
            options={graphOptions}
            onNodeClick={handleNodeClick}
            onLineClick={handleLineClick}
          />
        </div>
      </Card>

      {/* 节点详情面板 */}
      {selectedNode && (
        <NodeDetail
          node={selectedNode}
          open={!!selectedNode}
          onOpenChange={(open) => !open && setSelectedNode(null)}
        />
      )}
    </div>
  );
}

// ==================== 工具函数 ====================

/**
 * 固定的类型颜色映射
 */
function getTypeColor(type: string): string {
  const colorMap: Record<string, string> = {
    query: "#1890ff",    // 蓝色
    entity: "#52c41a",   // 绿色（统一颜色，不再渐变）
    event: "#faad14",    // 黄色
    section: "#8c8c8c",  // 灰色
  };
  return colorMap[type] || "#666666";  // 默认深灰
}

/**
 * 获取节点显示文本（不含标签，标签由 HTML 模板单独渲染）
 */
function getNodeDisplayText(node: Node): string {
  // 事项显示更多内容，其他类型显示较少
  // 因为有自动换行，可以适当增加长度
  const maxLength = node.type === 'event' ? 25 : (node.type === 'query' ? 18 : 15);
  return truncateText(node.content, maxLength);
}

/**
 * 首字母大写
 */
function capitalizeFirst(text: string): string {
  if (!text) return text;
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
}

/**
 * 生成完全自定义的节点 HTML（正文 + 下方标签 + 阶段/hop信息）
 */
function generateNodeHtml(node: Node, customColor?: string): string {
  const content = getNodeDisplayText(node);
  const hasCategory = node.category && node.category.trim() !== '';
  // 🆕 支持传入自定义颜色（用于段落召回的紫色事项）
  const nodeColor = customColor || getTypeColor(node.type);

  // 🎨 entity节点显示hop信息
  const isEntity = node.type === 'entity';
  const entityHop = node.hop ?? 0;
  const entityHopLabel = entityHop === 0 ? 'Recall' : `第${entityHop}跳`;

  // 🎨 event节点显示stage信息
  const isEvent = node.type === 'event';
  let eventStageLabel = '';
  if (isEvent && node.stage) {
    if (node.stage === 'recall') {
      eventStageLabel = 'Recall';
    } else if (node.stage === 'expand') {
      const eventHop = node.hop ?? 1;
      eventStageLabel = `第${eventHop}跳`;
    } else if (node.stage === 'rerank') {
      eventStageLabel = 'Rerank';
    }
  }

  // 简单的 HTML 转义
  const safeContent = content.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  // 标签首字母大写
  const safeCategory = hasCategory ? capitalizeFirst(node.category).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') : '';

  // 根据节点类型设置最大宽度（统一协调）
  const maxWidth = node.type === 'event' ? '190px' : '160px';

  // 调试日志
  // if (node.type === 'event') {
  //   console.log('Event节点:', {
  //     id: node.id,
  //     content: node.content,
  //     category: node.category,
  //     hasCategory: hasCategory,
  //     safeCategory: safeCategory,
  //     stage: node.stage,
  //     hop: node.hop,
  //     stageLabel: eventStageLabel
  //   });
  // }

  return `
    <div style="
      width: 100%;
      height: 100%;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      background: transparent;
      padding: 4px;
      position: relative;
    ">
      ${isEntity ? `
        <!-- 🎨 Entity 跳数标签：显示在右上角 -->
        <div style="
          position: absolute;
          top: 0px;
          right: 5px;
          background: rgba(0, 0, 0, 0.7);
          color: #ffffff;
          padding: 2px 6px;
          border-radius: 4px;
          font-size: 9px;
          font-weight: 600;
          white-space: nowrap;
          z-index: 10;
        ">
          ${entityHopLabel}
        </div>
      ` : ''}

      ${isEvent && eventStageLabel ? `
        <!-- 🎨 Event 阶段标签：显示在右上角 -->
        <div style="
          position: absolute;
          top: 0px;
          right: 5px;
          background: rgba(250, 140, 22, 0.9);
          color: #ffffff;
          padding: 2px 6px;
          border-radius: 4px;
          font-size: 9px;
          font-weight: 600;
          white-space: nowrap;
          z-index: 10;
        ">
          ${eventStageLabel}
        </div>
      ` : ''}

      <!-- 正文块：彩色背景，固定宽度更协调 -->
      <div style="
        background: ${nodeColor};
        color: #ffffff;
        padding: 10px 14px;
        border-radius: 6px;
        font-size: 13px;
        font-weight: 600;
        text-align: center;
        line-height: 1.5;
        width: ${maxWidth};
        min-height: 42px;
        display: flex;
        align-items: center;
        justify-content: center;
        word-wrap: break-word;
        overflow-wrap: break-word;
        white-space: normal;
        box-shadow: 0 2px 4px rgba(0,0,0,0.12);
      ">
        ${safeContent}
      </div>

      <!-- 下方标签：浅灰色半透明，紧凑优雅 -->
      ${hasCategory ? `
        <div style="
          margin-top: 4px;
          background: rgba(158, 169, 180, 0.6);
          color: #ffffff;
          padding: 2px 10px;
          border-radius: 8px;
          font-size: 10px;
          font-weight: 500;
          white-space: nowrap;
          letter-spacing: 0.5px;
        ">
          ${safeCategory}
        </div>
      ` : ''}
    </div>
  `;
}

/**
 * 截断文本
 */
function truncateText(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + "...";
}

/**
 * 精简模式：折叠每个阶段的中间路径，保留关键节点
 *
 * 核心逻辑：
 * - Recall: query → entity（折叠中间的 event 等）
 * - Expand: entity → entity（折叠中间的多跳路径）
 * - Rerank: 类似处理
 *
 * 关键：保留 entity 节点作为阶段连接点，确保阶段间不断开
 */
function simplifyClues(clues: Clue[]): Clue[] {
  if (clues.length === 0) return clues;

  // 按阶段分组
  const stageGroups: Record<string, Clue[]> = {
    prepare: [],
    recall: [],
    expand: [],
    rerank: [],
  };

  clues.forEach(clue => {
    if (stageGroups[clue.stage]) {
      stageGroups[clue.stage].push(clue);
    }
  });

  const simplifiedClues: Clue[] = [];
  const stages = ['prepare', 'recall', 'expand', 'rerank'];

  stages.forEach(stage => {
    const stageClues = stageGroups[stage];
    if (stageClues.length === 0) return;

    // 收集所有节点及其类型
    const nodeMap = new Map<string, Node>();
    const nodeOutEdges = new Map<string, Set<string>>();
    const nodeInEdges = new Map<string, Set<string>>();

    stageClues.forEach(clue => {
      nodeMap.set(clue.from.id, clue.from);
      nodeMap.set(clue.to.id, clue.to);

      if (!nodeOutEdges.has(clue.from.id)) {
        nodeOutEdges.set(clue.from.id, new Set());
      }
      nodeOutEdges.get(clue.from.id)!.add(clue.to.id);

      if (!nodeInEdges.has(clue.to.id)) {
        nodeInEdges.set(clue.to.id, new Set());
      }
      nodeInEdges.get(clue.to.id)!.add(clue.from.id);
    });

    // 识别关键节点（基于类型和位置）
    let startNodes: string[] = [];
    let endNodes: string[] = [];

    if (stage === 'prepare') {
      // Prepare 阶段：起点 = query (origin)，终点 = query (rewrite) 或 entity (extracted)
      startNodes = Array.from(nodeMap.values())
        .filter(node => node.type === 'query' && node.category === 'origin')
        .map(node => node.id);

      endNodes = Array.from(nodeMap.values())
        .filter(node =>
          (node.type === 'query' && node.category === 'rewrite') ||
          node.type === 'entity'
        )
        .map(node => node.id);

      // Prepare 阶段通常保留所有连线，不做精简
      if (startNodes.length === 0 || endNodes.length === 0) {
        simplifiedClues.push(...stageClues);
        return;
      }
    } else if (stage === 'recall') {
      // Recall 阶段：起点 = query，终点 = entity
      startNodes = Array.from(nodeMap.values())
        .filter(node => node.type === 'query')
        .map(node => node.id);

      endNodes = Array.from(nodeMap.values())
        .filter(node => node.type === 'entity')
        .map(node => node.id);
    } else if (stage === 'expand') {
      // Expand 阶段：起点 = entity（入度为0或小），终点 = entity（出度为0或小）
      const entities = Array.from(nodeMap.values()).filter(node => node.type === 'entity');

      startNodes = entities
        .filter(node => !nodeInEdges.has(node.id) || nodeInEdges.get(node.id)!.size === 0)
        .map(node => node.id);

      endNodes = entities
        .filter(node => !nodeOutEdges.has(node.id) || nodeOutEdges.get(node.id)!.size === 0)
        .map(node => node.id);

      // 如果没找到明确的起点/终点，使用所有 entity
      if (startNodes.length === 0) {
        startNodes = entities.map(n => n.id);
      }
      if (endNodes.length === 0) {
        endNodes = entities.map(n => n.id);
      }
    } else {
      // Rerank 阶段：保留所有连接
      startNodes = Array.from(nodeMap.values())
        .filter(node => !nodeInEdges.has(node.id) || nodeInEdges.get(node.id)!.size === 0)
        .map(node => node.id);

      endNodes = Array.from(nodeMap.values())
        .filter(node => !nodeOutEdges.has(node.id) || nodeOutEdges.get(node.id)!.size === 0)
        .map(node => node.id);
    }

    // 如果没有起点或终点，保留原始线索
    if (startNodes.length === 0 || endNodes.length === 0) {
      simplifiedClues.push(...stageClues);
      return;
    }

    // 计算被折叠的节点数
    const allNodeIds = new Set(nodeMap.keys());
    const keyNodeIds = new Set([...startNodes, ...endNodes]);
    const middleNodesCount = allNodeIds.size - keyNodeIds.size;

    // 创建折叠的连线：起点 → 终点
    const addedPairs = new Set<string>();

    startNodes.forEach(startId => {
      endNodes.forEach(endId => {
        if (startId === endId) return; // 跳过自环

        const pairKey = `${startId}-${endId}`;
        if (addedPairs.has(pairKey)) return;
        addedPairs.add(pairKey);

        const startNode = nodeMap.get(startId);
        const endNode = nodeMap.get(endId);

        if (startNode && endNode) {
          simplifiedClues.push({
            id: `${startId}-${endId}-simplified`,
            from: startNode,
            to: endNode,
            relation: middleNodesCount > 0 ? `+${middleNodesCount}` : '',
            confidence: 1.0,
            stage: stage as 'prepare' | 'recall' | 'expand' | 'rerank',
            metadata: {},
          });
        }
      });
    });
  });

  return simplifiedClues;
}

/**
 * 将线索数据转换为 relation-graph 格式 - 动态版
 */
function convertToGraphData(clues: Clue[]): GraphData {
  const nodesMap = new Map<string, GraphNode>();
  const lines: GraphLine[] = [];

  // 🆕 用于去重：from_id + to_id → line
  const linesMap = new Map<string, GraphLine>();

  // Stage 样式配置（相对固定）
  const stageStyles: Record<string, { color: string; width: number }> = {
    prepare: { color: "#722ed1", width: 2.0 },  // 紫色 - 准备阶段
    recall: { color: "#1890ff", width: 2.5 },   // 蓝色 - 召回
    expand: { color: "#52c41a", width: 2.0 },   // 绿色 - 扩展
    rerank: { color: "#fa8c16", width: 3.0 },   // 橙色 - 重排
  };

  // 🆕 第一遍：识别最终结果事项（必须同时满足：rerank阶段 + display_level="final" + event类型）
  const finalEvents = new Set<string>();
  clues.forEach((clue) => {
    // 严格限制：必须是 rerank 阶段、display_level="final"、终点是 event 类型
    if (clue.stage === 'rerank' &&
        clue.display_level === 'final' &&
        clue.to.type === 'event') {
      finalEvents.add(clue.to.id);
      console.log('🎯 发现 final 事项 (rerank阶段):', clue.to.id, clue.to.content);
    }
  });

  console.log('📊 Final 事项统计 (仅rerank阶段):', {
    total: finalEvents.size,
    ids: Array.from(finalEvents).map(id => id.substring(0, 8) + '...')
  });

  // 提取所有节点和连线
  clues.forEach((clue) => {
    // 添加起点节点
    if (!nodesMap.has(clue.from.id)) {
      // 🆕 判断是否是最终结果事项（紫色）
      const isPurpleEvent = clue.from.type === 'event' && finalEvents.has(clue.from.id);
      const typeColor = isPurpleEvent ? "#9c27b0" : getTypeColor(clue.from.type);

      if (isPurpleEvent) {
        console.log('🟣 设置紫色节点 (from):', clue.from.id.substring(0, 8) + '...', clue.from.content);
      }

      nodesMap.set(clue.from.id, {
        id: clue.from.id,
        text: '',  // 置空，使用 HTML
        html: generateNodeHtml(clue.from, typeColor),  // 传入颜色
        type: clue.from.type,
        color: 'transparent',  // 透明背景
        fontColor: "#ffffff",
        fontSize: 12,
        // 统一尺寸，event 稍大
        width: clue.from.type === 'event' ? 210 : 180,
        height: clue.from.type === 'event' ? 90 : 85,
        borderWidth: 0,
        borderColor: 'transparent',
        data: clue.from,
      });
    }

    // 添加终点节点
    if (!nodesMap.has(clue.to.id)) {
      // 🆕 判断是否是最终结果事项（紫色）
      const isPurpleEvent = clue.to.type === 'event' && finalEvents.has(clue.to.id);
      const typeColor = isPurpleEvent ? "#9c27b0" : getTypeColor(clue.to.type);

      if (isPurpleEvent) {
        console.log('🟣 设置紫色节点 (to):', clue.to.id.substring(0, 8) + '...', clue.to.content);
      }

      nodesMap.set(clue.to.id, {
        id: clue.to.id,
        text: '',  // 置空，使用 HTML
        html: generateNodeHtml(clue.to, typeColor),  // 传入颜色
        type: clue.to.type,
        color: 'transparent',  // 透明背景
        fontColor: "#ffffff",
        fontSize: 12,
        width: clue.to.type === 'event' ? 210 : 180,
        height: clue.to.type === 'event' ? 90 : 85,
        borderWidth: 0,
        borderColor: 'transparent',
        data: clue.to,
      });
    }

    // 添加连线（显示阶段名称）
    let stageStyle = stageStyles[clue.stage] || { color: "#90caf9", width: 2 };

    // 🆕 特殊处理：rerank阶段的线条颜色
    if (clue.stage === 'rerank') {
      // Section → Event: 紫色（段落召回事项）
      if (clue.from.type === 'section' && clue.to.type === 'event') {
        stageStyle = { color: "#9c27b0", width: 2.5 };  // 🟣 紫色
      }
      // Entity → Event: 青色
      else if (clue.from.type === 'entity' && clue.to.type === 'event') {
        stageStyle = { color: "#00bcd4", width: 2.5 };  // 青色
      }
      // Event → Section: 蓝色
      else if (clue.from.type === 'event' && clue.to.type === 'section') {
        stageStyle = { color: "#2196f3", width: 2.5 };  // 蓝色
      }
      // 其他关系: 浅灰色
      else {
        stageStyle = { color: "#bdbdbd", width: 2.0 };  // 浅灰色
      }
    }

    // 阶段中文名称映射
    const stageLabels: Record<string, string> = {
      prepare: '准备',
      recall: '召回',
      expand: '拓展',
      rerank: '重排',
    };

    const lineText = stageLabels[clue.stage] || clue.stage;

    // 🆕 去重：同一个 from → to 只添加一条线
    const lineKey = `${clue.from.id}->${clue.to.id}`;
    if (!linesMap.has(lineKey)) {
      const line = {
        from: clue.from.id,
        to: clue.to.id,
        text: lineText,
        color: stageStyle.color,
        lineWidth: stageStyle.width,
        fontColor: stageStyle.color,
        data: clue,
      };
      linesMap.set(lineKey, line);
    }
  });

  // 查找 rootId：选择第一个 query 类型的节点
  let rootId = '';
  for (const node of nodesMap.values()) {
    if (node.type === 'query') {
      rootId = node.id;
      break;
    }
  }
  // 如果没有 query 节点，使用第一个节点
  if (!rootId && nodesMap.size > 0) {
    rootId = Array.from(nodesMap.values())[0].id;
  }

  return {
    rootId,
    nodes: Array.from(nodesMap.values()),
    lines: Array.from(linesMap.values()),  // 🆕 使用去重后的线索
  };
}
