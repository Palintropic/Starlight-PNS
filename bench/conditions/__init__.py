# bench/conditions/ — 三个条件的上下文组装。
#
# BENCH-1 的实验设计：三个条件只许在"上下文怎么组装"这一件事上不同，
# 生成链（pns.logic.simulation.call_character）、模型、参数、角色材料、
# 探针问题全部共用。所以这里每个模块只产出 history（消息列表），
# 不碰 provider、不做判分。
