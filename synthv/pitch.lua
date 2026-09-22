--[[
原生音高曲线适配器，仅返回模块表，不注册菜单、定时器或 IPC。

坐标依据 Dreamtonics 的 PitchControlCurve / NoteGroupReference 文档：
曲线锚点时间相对音符组，锚点音高相对组移调；曲线内部点再相对这两个锚点。
输入 value 明确为工程绝对 MIDI 半音，绝不把 computedPitch 猜作可重复叠加的基线。
预览只创建独立对象；工程写入、撤销记录、跨组保护和失败回滚由桥接调用层编排。
]]

local M = {}
local MAX_POINTS, MAX_FINGERPRINT, MAX_METADATA = 4000, 262144, 32768
local MAX_SAFE = 9007199254740991
local safeErrors={}

local function fail(message)
  -- 只登记本模块生成的固定中文提示，describe 不会把未知宿主异常当成可展示文本。
  safeErrors[message]=true
  error(message,0)
end
local function finite(value)
  return type(value)=="number" and value==value and value~=math.huge and value~=-math.huge
end

local function method(object, name)
  -- 宿主对象可能是 userdata；未知成员的访问也可能抛异常，因此不能直接探测字段。
  local ok, value = pcall(function() return object[name] end)
  if not ok then return nil,"访问失败" end
  -- SynthV 2.2.1 的方法绑定实际可以是带 __call 的 userdata，并非普通 Lua function。
  -- 这里只接受这两类候选；userdata 是否可调用由 invoke 的受保护实际调用判定。
  if type(value)=="function" or type(value)=="userdata" then return value end
  -- 仅暴露 Lua 类型名以定位绑定差异，不输出成员值或宿主异常中的工程信息。
  return nil,type(value)
end

local function invoke(object, name, ...)
  local callback,kind = method(object, name)
  -- name 全部来自本文件的固定接口调用，不来自参数、工程或脚本元数据。
  if not callback then fail("当前宿主缺少可调用的原生音高接口 "..name.."（成员类型："..kind.."），未继续操作。") end
  -- 显式经包装函数执行调用，使可调用 userdata 与常规冒号调用走同一 Lua 语义。
  -- 不读取宿主元表（它可能受保护），也不把 userdata 的 tostring 输出暴露给用户。
  local ok, result = pcall(function(target,...) return callback(target,...) end, object, ...)
  if not ok then fail("宿主原生音高接口调用失败（"..name.."），未确认操作成功。") end
  return result
end

local function length(value, maximum, message)
  -- 明确拒绝稀疏数组和额外键，不能让 Lua 的 # 或 ipairs 静默截断请求/快照。
  if type(value)~="table" then fail(message) end
  local count = 0
  for key in pairs(value) do
    if not finite(key) or key<1 or key%1~=0 or key>maximum then fail(message) end
    count=count+1
  end
  if count>maximum or count~=#value then fail(message) end
  return count
end

local function stable(value, byteLimit)
  -- 不依赖 JSON 对象顺序；长度前缀避免字符串中的分隔符造成指纹碰撞。
  -- 整数 1 和浮点 1.0 使用相同数值表示，同时保留浮点读回的实际精度。
  local seen, pieces, bytes, nodes = {}, {}, 0, 0
  local function append(part)
    bytes=bytes+#part
    if bytes>byteLimit then fail("原生音高快照或脚本元数据过大，已拒绝处理。") end
    pieces[#pieces+1]=part
  end
  local encode
  encode=function(item, depth)
    nodes=nodes+1
    if depth>16 or nodes>100000 then fail("原生音高脚本元数据过于复杂，已拒绝处理。") end
    local kind=type(item)
    if kind=="nil" then append("z")
    elseif kind=="boolean" then append(item and "t" or "f")
    elseif kind=="number" then
      if not finite(item) then fail("原生音高包含非有限数值，已拒绝处理。") end
      -- 有些嵌入式 Lua 会裁剪标准库；math.type 缺失不应让本可读取的接口失效。
      -- 数字仍按完整 double 精度编码，具备整数子类型时再保留其整数表示。
      local integer=type(math.type)=="function" and math.type(item)=="integer"
      local numeric=item==0 and "0" or (integer and tostring(item) or string.format("%.17g",item))
      append("n"..numeric..";")
    elseif kind=="string" then append("s"..#item..":"..item)
    elseif kind=="table" then
      if seen[item] then fail("原生音高脚本元数据包含循环引用，已拒绝处理。") end
      seen[item]=true
      local keys={}
      for key in pairs(item) do
        if type(key)~="string" and not (finite(key) and key%1==0) then
          fail("原生音高脚本元数据类型不受支持，已拒绝处理。")
        end
        keys[#keys+1]=key
        if #keys>MAX_POINTS*4 then fail("原生音高脚本元数据项过多，已拒绝处理。") end
      end
      table.sort(keys,function(a,b)
        if type(a)~=type(b) then return type(a)<type(b) end
        return a<b
      end)
      append("{")
      for _,key in ipairs(keys) do encode(key,depth+1); encode(item[key],depth+1) end
      append("}"); seen[item]=nil
    else fail("原生音高脚本元数据类型不受支持，已拒绝处理。") end
  end
  encode(value,0)
  return table.concat(pieces)
end

local function scriptData(object)
  local keysMethod, valueMethod=method(object,"getScriptDataKeys"),method(object,"getScriptData")
  -- 较旧绑定可能不暴露脚本元数据；存在接口却读取失败时必须拒绝，不能假装没有数据。
  if not keysMethod and not valueMethod then return false,"" end
  if not keysMethod or not valueMethod then fail("原生音高脚本元数据无法完整读取，已拒绝处理。") end
  local keys=invoke(object,"getScriptDataKeys")
  length(keys,MAX_POINTS,"原生音高脚本元数据键列表无效或过大。")
  local values={}
  for _,key in ipairs(keys) do
    if type(key)~="string" or values[key]~=nil then fail("原生音高脚本元数据键无效。") end
    -- 包装值以区分不存在的键和返回 nil 的已列出键。
    values[key]={value=invoke(object,"getScriptData",key)}
  end
  return true,stable(values,MAX_METADATA)
end

local function describeControl(control)
  local position,pitch=invoke(control,"getPosition"),invoke(control,"getPitch")
  if not finite(position) or math.abs(position)>MAX_SAFE or not finite(pitch) then
    fail("原生音高控件的坐标无效，已拒绝处理。")
  end
  local row={kind=method(control,"getPoints") and "curve" or "point",position=position,pitch=pitch}
  if row.kind=="curve" then
    local points=invoke(control,"getPoints")
    length(points,MAX_POINTS,"原生音高曲线超过4000点或点列表无效。")
    row.points={}
    for _,point in ipairs(points) do
      if length(point,2,"原生音高曲线点格式无效。")~=2
          or not finite(point[1]) or math.abs(point[1])>MAX_SAFE or not finite(point[2]) then
        fail("原生音高曲线点坐标无效。")
      end
      row.points[#row.points+1]={point[1],point[2]}
    end
  end
  row.metadataReadable,row.metadata=scriptData(control)
  return row
end

local function packed(controls)
  local rows,total={},0
  if #controls>MAX_POINTS then fail("原生音高控件总量超过4000，已拒绝处理。") end
  for _,entry in ipairs(controls) do
    rows[#rows+1]=entry.description
    total=total+(entry.description.kind=="curve" and math.max(1,#entry.description.points) or 1)
    if total>MAX_POINTS then fail("原生音高控制点总量超过4000，已拒绝处理。") end
  end
  return {controls=controls,pointCount=total,fingerprint=stable(rows,MAX_FINGERPRINT)}
end

local function clonedEntry(object)
  local description=describeControl(object)
  local copy=invoke(object,"clone")
  if stable(describeControl(copy),MAX_FINGERPRINT)~=stable(description,MAX_FINGERPRINT) then
    fail("宿主克隆未保留完整原生音高数据，已拒绝处理。")
  end
  return {clone=copy,description=description}
end

local function addReadBudget(description, total, bytes)
  -- 在遍历过程中限制累计成本，不能等到数千条大曲线全部克隆后才发现总量越界。
  -- 最终指纹还会单独检查结构开销；此处先阻止大量点或元数据占用不受控内存。
  total=total+(description.kind=="curve" and math.max(1,#description.points) or 1)
  bytes=bytes+#stable(description,MAX_FINGERPRINT)
  if total>MAX_POINTS then fail("原生音高控制点总量超过4000，已拒绝处理。") end
  if bytes>MAX_FINGERPRINT then fail("原生音高快照或脚本元数据过大，已拒绝处理。") end
  return total,bytes
end

local function capture(group, clone)
  local count=invoke(group,"getNumPitchControls")
  if not finite(count) or count<0 or count%1~=0 or count>MAX_POINTS then
    fail("原生音高控件总量超过4000或数量无效。")
  end
  local controls,total,bytes={},0,0
  for index=1,count do
    local object=invoke(group,"getPitchControl",index)
    controls[index]=clone and clonedEntry(object) or {description=describeControl(object)}
    total,bytes=addReadBudget(controls[index].description,total,bytes)
  end
  return packed(controls)
end

function M.snapshot(group)
  -- 快照的 clone 不附加到工程，保留宿主不可枚举的内部信息以及可读脚本元数据。
  return capture(group,true)
end

local function deltaState(group)
  local curve=invoke(group,"getParameter","pitchDelta")
  local points=invoke(curve,"getAllPoints")
  length(points,MAX_POINTS,"音高偏移曲线超过4000点或无法完整读取。")
  local rows={}
  for _,point in ipairs(points) do
    if length(point,2,"音高偏移控制点格式无效。")~=2 or not finite(point[1]) or not finite(point[2]) then
      fail("音高偏移控制点包含无效数值。")
    end
    rows[#rows+1]={point[1],point[2]}
  end
  local interpolation=invoke(curve,"getInterpolationMethod")
  local definition=invoke(curve,"getDefinition")
  if type(interpolation)~="string" or type(definition)~="table" or not finite(definition.defaultValue) then
    fail("音高偏移参数定义无效，已拒绝原生音高预览。")
  end
  return curve,rows,interpolation,stable({points=rows,method=interpolation,default=definition.defaultValue},MAX_FINGERPRINT)
end

function M.same(group, snapshot)
  -- 指纹覆盖顺序、类型、锚点、内部曲线和可读元数据；读失败同样视为不一致。
  local ok,result=pcall(function()
    if type(snapshot)~="table" or capture(group,false).fingerprint~=snapshot.fingerprint then return false end
    if snapshot.pitchDeltaFingerprint then
      local _,_,_,fingerprint=deltaState(group)
      if fingerprint~=snapshot.pitchDeltaFingerprint then return false end
    end
    return true
  end)
  return ok and result or false
end

local interpolationMode
local function calibratedInterpolationMode()
  -- 官方文档描述相对坐标，但 2.2.1 实测为组内时间与已含锚点的组内音高。
  -- 使用固定的独立曲线辨识这两种语义；只缓存成功结果，绝不挂接或读取工程曲线。
  if interpolationMode then return interpolationMode end
  local curve=invoke(SV,"create","PitchControlCurve")
  invoke(curve,"setPosition",1000000000)
  invoke(curve,"setPitch",60)
  invoke(curve,"setPoints",{{0,0},{100000000,2}})
  local position,pitch=invoke(curve,"getPosition"),invoke(curve,"getPitch")
  if position~=1000000000 or pitch~=60 then fail("原生音高坐标校准的锚点读回不一致。") end
  local points=invoke(curve,"getPoints")
  if length(points,2,"原生音高坐标校准的点列表无效。")~=2 then fail("原生音高坐标校准的点数量无效。") end
  for index,point in ipairs(points) do
    if length(point,2,"原生音高坐标校准的点格式无效。")~=2
        or point[1]~=(index-1)*100000000 or point[2]~=(index-1)*2 then
      fail("原生音高坐标校准的控制点读回不一致。")
    end
  end
  local samples={}
  for index,time in ipairs({0,50000000,100000000,1000000000,1050000000,1100000000}) do
    -- 不同语义下另一组位置可能越界，允许其返回 NaN 或异常；这些内容不对外输出。
    local ok,value=pcall(invoke,curve,"getValueAt",time)
    samples[index]=ok and finite(value) and value or false
  end
  local function matches(start,expected)
    for index,value in ipairs(expected) do
      local actual=samples[start+index-1]
      if not finite(actual) or math.abs(actual-value)>0.00001 then return false end
    end
    return true
  end
  local relative,group=matches(1,{0,1,2}),matches(4,{60,61,62})
  if relative==group then fail("原生音高插值坐标语义无法唯一校准，已拒绝处理。") end
  interpolationMode=relative and "relative" or "group"
  return interpolationMode
end

function M.describe(group, ref)
  local phase="native-controls"
  local labels={
    ["native-controls"]="读取原生音高控件",
    ["group-offsets"]="读取音符组时间与移调",
    ["pitch-delta"]="读取音高偏移依赖",
    ["interpolation"]="校准原生音高插值坐标",
    ["fingerprint"]="生成原生音高快照摘要",
  }
  local state
  local ok,result=pcall(function()
    state=capture(group,false)
    phase="group-offsets"
    local offset,transpose=invoke(ref,"getTimeOffset"),invoke(ref,"getPitchOffset")
    if not finite(offset) or not finite(transpose) then fail("音符组偏移无效。") end
    phase="pitch-delta"
    local _,_,_,delta=deltaState(group)
    phase="interpolation"
    local mode=calibratedInterpolationMode()
    phase="fingerprint"
    return {available=true,pointCount=state.pointCount,controlCount=#state.controls,
      fingerprint=stable({native=state.fingerprint,pitchDelta=delta,timeOffset=offset,pitchOffset=transpose,coordinateMode=mode},MAX_FINGERPRINT)}
  end)
  if ok then return result end
  -- 只可透传模块自己的安全提示；意外 Lua/绑定异常一律用固定文本替代。
  -- 已读到控件后再失败时保留真实数量，但不能用它宣称整项能力可用。
  local detail=type(result)=="string" and safeErrors[result] and result or "宿主接口返回了无法安全处理的数据。"
  return {available=false,pointCount=state and state.pointCount or 0,
    controlCount=state and #state.controls or nil,fingerprint="",reasonCode=phase,
    message="原生音高能力检查失败（"..labels[phase].."）："..detail}
end

function M.write(group, snapshot)
  -- 先准备所有独立副本再删除宿主对象；即使中途失败，传入快照仍完整可供调用层回滚。
  if type(snapshot)~="table" or type(snapshot.controls)~="table" then fail("原生音高恢复快照无效。") end
  length(snapshot.controls,MAX_POINTS,"原生音高恢复快照超过4000项或格式无效。")
  local fresh,total,bytes={},0,0
  for _,entry in ipairs(snapshot.controls) do
    if type(entry)~="table" or not entry.clone then fail("原生音高恢复快照缺少宿主副本。") end
    fresh[#fresh+1]=clonedEntry(entry.clone)
    total,bytes=addReadBudget(fresh[#fresh].description,total,bytes)
  end
  if packed(fresh).fingerprint~=snapshot.fingerprint then fail("原生音高恢复快照已变化，未写入。") end
  -- 偏移曲线是候选绝对音高成立的依赖；已知依赖变化时不先删除任何原生控件。
  -- 不比较当前原生控件与目标快照，因为恢复操作本来就要把不同的当前状态恢复回去。
  if snapshot.pitchDeltaFingerprint then
    local _,_,_,fingerprint=deltaState(group)
    if fingerprint~=snapshot.pitchDeltaFingerprint then fail("音高偏移曲线已变化，请重新读取选区并预览。") end
  end
  -- 工程对象必须具备完整读写接口；缺少添加接口时不能先清空原控件。
  if not method(group,"removePitchControl") or not method(group,"addPitchControl") then
    fail("当前宿主缺少原生音高写入接口，未继续操作。")
  end
  local count=invoke(group,"getNumPitchControls")
  if not finite(count) or count<0 or count%1~=0 or count>MAX_POINTS then fail("原生音高控件数量无效。") end
  for index=count,1,-1 do invoke(group,"removePitchControl",index) end
  for _,entry in ipairs(fresh) do invoke(group,"addPitchControl",entry.clone) end
  if not M.same(group,snapshot) then fail("原生音高写入后完整校验失败，请由桥接恢复原快照。") end
  return true
end

local function inspectDelta(group, begin, finish)
  -- 原生控制曲线与 pitchDelta 是两个独立编辑对象；已有偏移不妨碍读取、克隆
  -- 和预览原生曲线。这里只检查依赖是否可完整读取，绝不清零、相减或猜测合成
  -- 顺序。预览明确展示原生控制值，而不是声称已测得最终音频基频。
  local curve,points,interpolation,fingerprint=deltaState(group)
  local kind=interpolation:lower()
  if kind~="linear" and kind~="cosine" and kind~="cubic" then
    fail("音高偏移插值方式未知，无法可靠读取原生音高依赖。")
  end
  local nonzero=false
  local positions={[begin]=true,[finish]=true}
  for _,point in ipairs(points) do if point[1]>begin and point[1]<finish then positions[point[1]]=true end end
  local breaks={}; for position in pairs(positions) do breaks[#breaks+1]=position end
  table.sort(breaks)
  -- 将旧断点分段后检查端点与三个内部点，覆盖三次插值由区外点带来的内部弯曲。
  -- 非零值仅产生保留说明；非有限值仍拒绝。完整指纹继续参与应用和恢复校验，
  -- 因而用户在预览后编辑旧偏移时，不能应用一个依赖已失效的候选。
  for index=1,#breaks-1 do
    for sample=0,4 do
      local value=invoke(curve,"get",breaks[index]+(breaks[index+1]-breaks[index])*sample/4)
      if not finite(value) then fail("无法可靠读取选区内的音高偏移，未生成原生音高预览。") end
      nonzero=nonzero or value~=0
    end
  end
  return fingerprint,nonzero
end

local preservedDeltaWarning="选区内已有音高偏移，将原样保留；图中显示新原生控制曲线，不代表最终合成音高，请应用后试听。"

function M.selectionAvailability(group, begin, finish)
  -- 保留 describe 的完整指纹，能力判断与预览采用相同依赖读取检查；已有非零
  -- 偏移仅提示保留，不再把「控制曲线预览」误当成必须先合成音频才能完成的操作。
  if not finite(begin) or not finite(finish) or finish<=begin then return {available=true} end
  local ok,result,nonzero=pcall(inspectDelta,group,begin,finish)
  if ok then return {available=true,message=nonzero and preservedDeltaWarning or nil} end
  return {available=false,code="pitch-delta-unknown",
    message="无法确认选区内音高偏移的兼容状态，原生音高曲线暂不可用；请使用音高偏移（pitchDelta）或检查宿主曲线。"}
end

local function validateInput(args, ctx)
  if type(args)~="table" or args.parameter~="pitchCurve" then fail("原生音高参数必须为 pitchCurve。") end
  for key in pairs(args) do
    if key~="parameter" and key~="curve" and key~="renderMode" then fail("原生音高只接受绝对曲线，不接受 delta 或未知字段。") end
  end
  local mode=args.renderMode==nil and "smooth" or args.renderMode
  if mode=="points" then
    fail("原生音高点会影响邻近生成音高，请使用连续音高曲线；若需密集点请选音高偏移参数。")
  end
  if mode~="smooth" then fail("原生音高仅支持 smooth 连续曲线模式。") end
  local count=length(args.curve,64,"原生音高曲线必须含2至64个连续点。")
  if count<2 then fail("原生音高曲线必须含2至64个连续点。") end
  if type(ctx)~="table" or not finite(ctx.begin) or not finite(ctx.finish) or ctx.finish<=ctx.begin
      or math.abs(ctx.begin)>MAX_SAFE or math.abs(ctx.finish)>MAX_SAFE
      or not finite(ctx.startSeconds) or not finite(ctx.endSeconds) or ctx.endSeconds<=ctx.startSeconds
      or ctx.endSeconds-ctx.startSeconds>30 then fail("原生音高选区时间无效或超过30秒。") end
  local notes=type(ctx.selection)=="table" and ctx.selection.notes
  local noteCount=length(notes,128,"原生音高需要1至128个有效选中音符。")
  if noteCount<1 then fail("原生音高需要1至128个有效选中音符。") end
  local transpose=invoke(ctx.ref,"getPitchOffset")
  local timeOffset=invoke(ctx.ref,"getTimeOffset")
  if not finite(transpose) or not finite(timeOffset) then fail("音符组移调或时间偏移无效。") end
  local low,high=math.huge,-math.huge
  for _,note in ipairs(notes) do
    if type(note)~="table" or not finite(note.pitch) then fail("选中音符的音高无效。") end
    low=math.min(low,note.pitch+transpose-2); high=math.max(high,note.pitch+transpose+2)
  end
  low,high=math.max(0,low),math.min(127,high)
  local previous=-1
  for _,point in ipairs(args.curve) do
    if length(point,2,"原生音高点必须为时间比例和绝对MIDI音高二元数组。")~=2
        or not finite(point[1]) or point[1]<0 or point[1]>1 or point[1]<=previous
        or not finite(point[2]) or point[2]<low or point[2]>high then
      fail("原生音高点需按0至1严格递增，音高须在0至127及选区音符范围上下2半音内。")
    end
    previous=point[1]
  end
  if args.curve[1][1]~=0 or args.curve[count][1]~=1 then fail("原生音高曲线首尾时间比例必须为0和1。") end
  return transpose,timeOffset,low,high,noteCount
end

function M.preview(args, ctx)
  local transpose,timeOffset,low,high,noteCount=validateInput(args,ctx)
  local coordinateMode=calibratedInterpolationMode()
  local before=M.snapshot(ctx.group)
  local deltaFingerprint,hasExistingDelta=inspectDelta(ctx.group,ctx.begin,ctx.finish)
  before.pitchDeltaFingerprint=deltaFingerprint
  local kept,replaced={},0
  for _,entry in ipairs(before.controls) do
    local row=entry.description
    if row.kind=="point" then
      if row.position>=ctx.begin and row.position<=ctx.finish then
        fail("选区内已有原生音高引导点；移除它可能影响邻近音高，请先手动处理。")
      end
      kept[#kept+1]=entry
    else
      -- SynthV 的绘制工具允许留下单节点甚至空的连续曲线；它们不等同于
      -- PitchControlPoint 引导点，不能套用新建候选至少两个节点的输入约束。
      -- 空曲线没有可替换的时间范围，因此保留完整克隆及脚本元数据。
      if #row.points==0 then kept[#kept+1]=entry
      else
        local first,last=math.huge,-math.huge
        for _,point in ipairs(row.points) do
          -- 内部节点可以使用负偏移；实际覆盖位置由锚点和节点偏移相加得到，
          -- 不能只看曲线锚点。单节点自然形成首尾相同的零长度范围。
          local position=row.position+point[1]
          if not finite(position) or math.abs(position)>MAX_SAFE then
            fail("现有原生音高曲线的实际时间位置无效，已拒绝预览。")
          end
          first=math.min(first,position); last=math.max(last,position)
        end
        if last<ctx.begin or first>ctx.finish then kept[#kept+1]=entry
        elseif first>=ctx.begin and last<=ctx.finish then replaced=replaced+1
        else fail("已有原生音高曲线跨越选区边界，已拒绝预览；请扩大选区或手动处理。") end
      end
    end
  end
  local points,previous={},nil
  for index,point in ipairs(args.curve) do
    local position
    if index==1 then position=ctx.begin
    elseif index==#args.curve then position=ctx.finish
    else
      local seconds=ctx.startSeconds+(ctx.endSeconds-ctx.startSeconds)*point[1]
      position=math.floor(invoke(ctx.axis,"getBlickFromSeconds",seconds)-timeOffset+0.5)
    end
    if not finite(position) or position<ctx.begin or position>ctx.finish or (previous and position<=previous) then
      fail("曲线时间点在宿主时间精度下重合或越界，请增加点间距离。")
    end
    points[#points+1]={position-ctx.begin,point[2]-args.curve[1][2]}
    previous=position
  end
  local curve=invoke(SV,"create","PitchControlCurve")
  invoke(curve,"setPosition",ctx.begin)
  invoke(curve,"setPitch",args.curve[1][2]-transpose)
  invoke(curve,"setPoints",points)
  -- 通过宿主读回候选：float32 量化或锚点规范化后的真实值才是应用校验目标。
  local added=clonedEntry(curve)
  local row=added.description
  if row.kind~="curve" or #row.points~=#points then fail("宿主规范化改变了曲线点数量，已拒绝预览。") end
  previous=nil
  for _,point in ipairs(row.points) do
    local position=row.position+point[1]
    local pitch=row.pitch+point[2]+transpose
    if not finite(position) or position<ctx.begin or position>ctx.finish or (previous and position<=previous)
        or not finite(pitch) or pitch<0 or pitch>127 or pitch<low-0.00001 or pitch>high+0.00001 then
      fail("宿主规范化后的原生音高曲线越界，已拒绝预览。")
    end
    previous=position
  end
  if row.position+row.points[1][1]~=ctx.begin or previous~=ctx.finish then
    fail("宿主规范化后的曲线未保留选区首尾，已拒绝预览。")
  end
  local function sampledPitch(position)
    -- 原生曲线没有公开插值类型设置器，不能用网页直线近似冒充宿主实际曲线。
    -- 组内语义已经含锚点音高，不能重复加 row.pitch；两种语义都只加一次组移调。
    local query=coordinateMode=="group" and position or position-row.position
    local value=invoke(added.clone,"getValueAt",query)
    local pitch=finite(value) and value+transpose+(coordinateMode=="group" and 0 or row.pitch) or nil
    if not finite(pitch) or pitch<0 or pitch>127 or pitch<low-0.00001 or pitch>high+0.00001 then
      fail("原生音高插值采样超出允许音高范围，已拒绝预览。")
    end
    return pitch
  end
  -- 固定曲线校准之外，再核验实际候选的全部节点，防止绑定/对象差异造成误判。
  -- 比較宿主已规范化的读回数据，而非原始浮点输入，允许 float32 的正常量化。
  for _,point in ipairs(row.points) do
    local expected=row.pitch+point[2]+transpose
    if math.abs(sampledPitch(row.position+point[1])-expected)>0.00001 then
      fail("原生音高候选节点与插值读回不一致，已拒绝预览。")
    end
  end
  -- 每个内部片段补查四个位置，防止密集断点间的异常被全选区等距采样漏掉。
  -- 文档没有给出原生插值的多项式形式，因此这里只承诺有限采样核验，不声称极值证明。
  for index=1,#row.points-1 do
    local left,right=row.points[index][1],row.points[index+1][1]
    for sample=1,4 do sampledPitch(row.position+left+(right-left)*sample/5) end
  end
  local previewRows={}
  for sample=0,96 do
    local fraction=sample/96
    local position
    if sample==0 then position=ctx.begin
    elseif sample==96 then position=ctx.finish
    else
      local seconds=ctx.startSeconds+(ctx.endSeconds-ctx.startSeconds)*fraction
      position=invoke(ctx.axis,"getBlickFromSeconds",seconds)-timeOffset
    end
    if not finite(position) or position<ctx.begin or position>ctx.finish then
      fail("宿主时间轴无法转换候选曲线。")
    end
    previewRows[#previewRows+1]={position=fraction,after=sampledPitch(position)}
  end
  -- 节点标记取自宿主规范化后的曲线描述，不能使用模型原始输入或 97 个插值采样。
  -- 横坐标把曲线锚点、节点偏移和组时间偏移换算为实际秒数；纵坐标将锚点音高、
  -- 节点相对音高与组移调相加一次，得到可与钢琴卷帘音符直接对齐的绝对 MIDI。
  -- 仅公开此次新建曲线的节点；保留在选区外的原生控件不属于本次预览的可视范围。
  local controlPoints,previousPosition={},-1
  for _,point in ipairs(row.points) do
    local groupPosition=row.position+point[1]
    local seconds=invoke(ctx.axis,"getSecondsFromBlick",groupPosition+timeOffset)
    local position=groupPosition==ctx.begin and 0 or (groupPosition==ctx.finish and 1
      or (seconds-ctx.startSeconds)/(ctx.endSeconds-ctx.startSeconds))
    if not finite(position) or position<0 or position>1 or position<=previousPosition then
      fail("宿主无法将原生音高节点转换为有效的预览时间位置。")
    end
    controlPoints[#controlPoints+1]={position=position,value=row.pitch+point[2]+transpose}
    previousPosition=position
  end
  kept[#kept+1]=added
  -- Lua table.sort 非稳定；显式保留同锚点旧控件的相对顺序，写后再次核对宿主顺序。
  local indices={}; for index,entry in ipairs(kept) do indices[entry]=index end
  table.sort(kept,function(a,b)
    if a.description.position==b.description.position then return indices[a]<indices[b] end
    return a.description.position<b.description.position
  end)
  local after=packed(kept)
  after.pitchDeltaFingerprint=before.pitchDeltaFingerprint
  return {before=before,after=after,public={parameter="pitchCurve",renderMode="smooth",noteCount=noteCount,
    startSeconds=ctx.startSeconds,endSeconds=ctx.endSeconds,beforePointCount=before.pointCount,
    pointCount=after.pointCount,controlCount=#after.controls,
    replacedControlCount=replaced,pitchRange={low,high},curvePreview=previewRows,controlPoints=controlPoints,beforeAvailable=false,
    capabilityWarnings=hasExistingDelta and {preservedDeltaWarning} or nil,
    summary="将按绝对MIDI音高写入连续原生曲线，替换完全位于选区内的原生曲线；已采样核验宿主插值，未读取生成音高作为基线，尚未写入。"}}
end

return M
