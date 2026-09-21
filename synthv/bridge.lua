--[[
SynthV Assistant：面向 Studio 2.2.1 的小范围桥接。
只执行下方白名单中的动作，不执行来自模型的 Lua/系统命令。
参数修改采用“预览 → 检查选区和原始曲线 → 应用”的流程；默认只读。
JSON 解析器及 IPC_DIR 由 Python 安装器注入本文件前部。
]]

function getClientInfo()
  return {name="SynthV Assistant", category="SynthV Assistant", author="Local", versionNumber=2, minEditorVersion=0x020102}
end

local session = tostring(os.time()) .. "-" .. tostring(math.floor(os.clock()*1000000))
local writeProject = nil
local previews, lastEdit = {}, nil
local counter, ticks, playbackToken = 0, 0, 0
local stopping=false
-- 声库尚未使用的模式不一定出现在 getVoice 中。用户可按面板名称补充本组目录；
-- 此表只活在桥接会话内，不写工程、声库或默认声线，也不接收模型自动注册。
local declaredModes={}
-- 通用参数与声库参数共用目录，但后者必须由当前宿主真实返回，不能按名称猜测。
local parameterOrder={"breathiness","tension","loudness","gender","pitchDelta","toneShift","vibratoEnv"}
local parameterSpecs={
  breathiness={label="气声",unit="参数值",limit=0.3,tolerance=0.002},
  tension={label="张力",unit="参数值",limit=0.3,tolerance=0.002},
  loudness={label="响度",unit="dB",limit=6,tolerance=0.05},
  gender={label="性别",unit="参数值",limit=0.3,tolerance=0.002},
  pitchDelta={label="音高偏移",unit="音分",limit=100,tolerance=0.5},
  toneShift={label="音色偏移",unit="音分",limit=200,tolerance=0.5},
  vibratoEnv={label="颤音包络",unit="参数值",limit=0.3,tolerance=0.002},
}
-- 限制预演成本，避免在编辑器的脚本回调中进行无界计算。
local MAX_CURVE_POINTS, OUTSIDE_EPSILON = 4000, 1e-7

local function readFile(name)
  local f = io.open(IPC_DIR .. "/" .. name, "rb")
  if not f then return nil end
  local value = f:read("*a"); f:close(); return value
end

local function writeFile(name, value)
  local path = IPC_DIR .. "/" .. name
  local f, err = io.open(path .. ".tmp", "wb")
  if not f then error("无法写入桥接目录：" .. tostring(err)) end
  f:write(json.encode(value)); f:close()
  -- Windows 上 rename 不覆盖目标。短暂的文件缺口由客户端轮询容忍。
  os.remove(path)
  local ok = os.rename(path .. ".tmp", path)
  if not ok then error("无法提交桥接响应。") end
end

local function finite(v)
  return type(v)=="number" and v==v and v~=math.huge and v~=-math.huge
end

local function samePoints(left,right)
  -- 数值相等即可；JSON 会把同值的整数/浮点数写成 1 与 1.0，不能用于宿主指纹。
  if type(left)~="table" or type(right)~="table" or #left~=#right then return false end
  for index,point in ipairs(left) do
    local other=right[index]
    if point[1]~=other[1] or point[2]~=other[2] then return false end
  end
  return true
end

local function stable(value)
  -- 宿主返回对象的键顺序不保证稳定；数值也可能在整数/浮点表示间变化。
  -- 此串仅用于本机冲突检测，不作为身份认证或公开的工程资料。
  if type(value)=="number" then return string.format("%.17g",value) end
  if type(value)~="table" then return json.encode(value) end
  local keys={}; for key,_ in pairs(value) do keys[#keys+1]=key end
  table.sort(keys,function(a,b) return tostring(a)<tostring(b) end)
  local parts={}; for _,key in ipairs(keys) do parts[#parts+1]=stable(key)..":"..stable(value[key]) end
  return "{"..table.concat(parts,",").."}"
end

local function fingerprint(value)
  -- 有界双散列只作为变更摘要；实际应用仍比较预览前后的完整数据，不能只信摘要。
  local raw=stable(value); local a,b=2166136261,5381
  for index=1,#raw do local byte=raw:byte(index); a=((a ~ byte)*16777619)&0xffffffff; b=(b*33+byte)&0xffffffff end
  return string.format("%08x%08x:%d",a,b,#raw)
end

local function projectName()
  return SV:getProject():getFileName() or ""
end

local function heartbeat()
  local info=SV:getHostInfo()
  writeFile("heartbeat.json", {session=session,timestamp=os.time(),hostVersion=info.hostVersion,
    projectFile=projectName(),writeEnabled=writeProject~=nil and writeProject==projectName(),protocol=2,
    capabilities={curves=true,nativePitch=NativePitch~=nil}})
end

local function current()
  local editor=SV:getMainEditor()
  local ref=editor:getCurrentGroup()
  if not ref then error("请先在 SynthV 打开一个音符组。") end
  return editor,ref,ref:getTarget(),SV:getProject():getTimeAxis()
end

local function voiceContext(editor,ref)
  -- 普通组可能只保存覆盖项，因此同时读取当前轨道主组的声线默认设置。
  -- 官方没有声库完整模式枚举接口；这里仅合并 getVoice 实际出现的模式名称。
  local inherited,localVoice={},{}
  pcall(function()
    local track=editor:getCurrentTrack()
    for index=1,track:getNumGroups() do
      local item=track:getGroupReference(index)
      if item:isMain() then inherited=item:getVoice() or {}; break end
    end
  end)
  pcall(function() localVoice=ref:getVoice() or {} end)
  local modes={}
  for _,voice in ipairs({inherited,localVoice}) do
    if type(voice.vocalModeParams)=="table" then
      for name,values in pairs(voice.vocalModeParams) do
        if type(name)=="string" and #name>0 and #name<=80 and not name:find("%c") and type(values)=="table" then
          modes[name]=values
        end
      end
    end
  end
  return modes,fingerprint({inherited,localVoice})
end

local function parameterCatalog(editor,ref,group)
  local modes,voiceFingerprint=voiceContext(editor,ref)
  local scope=fingerprint({projectName(),group:getUUID(),ref:getTimeOffset(),ref:getPitchOffset(),voiceFingerprint})
  local declared=declaredModes[scope] or {}
  for name in pairs(declared) do if not modes[name] then modes[name]={} end end
  local catalog={}
  local function register(name,spec,modeName)
    local ok,item=pcall(function()
      local curve=group:getParameter(name); local definition=curve:getDefinition(); local points=curve:getAllPoints()
      if type(definition.range)~="table" or not finite(definition.range[1]) or not finite(definition.range[2]) then return nil end
      -- 大曲线只报告不可编辑及点数；不为已拒绝的数据继续递归编码和逐字节散列。
      local digest=#points<=MAX_CURVE_POINTS and fingerprint({points,curve:getInterpolationMethod(),definition}) or "oversized:"..#points
      return {range=definition.range,defaultValue=definition.defaultValue,pointCount=#points,
        label=spec.label,unit=spec.unit,maxDelta=spec.limit,kind=modeName and "vocalMode" or "automation",modeName=modeName,
        available=#points<=MAX_CURVE_POINTS,fingerprint=digest,
        source=modeName and (declared[modeName] and "user" or "host") or "host"}
    end)
    if ok and item then catalog[name]=item end
  end
  for _,name in ipairs(parameterOrder) do register(name,parameterSpecs[name]) end
  local names={}; for name,_ in pairs(modes) do names[#names+1]=name end; table.sort(names)
  if #names>64 then error("声线模式数量超过当前目录上限，未读取编辑能力。") end
  for _,name in ipairs(names) do register("vocalMode_"..name,{label=name,unit="百分点",limit=30},name) end
  if NativePitch then
    local ok,details=pcall(NativePitch.describe,group,ref)
    catalog.pitchCurve={kind="pitch",label="原生音高曲线",unit="MIDI 半音",range={0,127},defaultValue=0,
      pointCount=ok and details.pointCount or 0,available=ok and details.available or false,
      fingerprint=ok and fingerprint(details.fingerprint) or "unavailable",
      unavailableReason=not (ok and details.available) and (ok and details.message or "原生音高能力检测失败。") or nil}
  end
  return catalog,voiceFingerprint,#names
end

local function selection()
  local editor,ref,group,axis=current()
  local notes, first, last = {}, nil, nil
  local selected=editor:getSelection():getSelectedNotes()
  table.sort(selected,function(a,b) return a:getOnset()<b:getOnset() end)
  for _,n in ipairs(selected) do
    local onset=n:getOnset()+ref:getTimeOffset()
    local finish=onset+n:getDuration()
    first=first and math.min(first,onset) or onset
    last=last and math.max(last,finish) or finish
    notes[#notes+1]={index=n:getIndexInParent(),pitch=n:getPitch(),lyrics=n:getLyrics(),
      onset=n:getOnset(),duration=n:getDuration(),onsetSeconds=axis:getSecondsFromBlick(onset),
      durationSeconds=axis:getSecondsFromBlick(finish)-axis:getSecondsFromBlick(onset)}
  end
  local curves,voiceFingerprint,modeCount=parameterCatalog(editor,ref,group)
  local pitchOffset=0; pcall(function() pitchOffset=ref:getPitchOffset() end)
  local warnings={modeCount==0 and "宿主尚未返回声线模式；可按 SynthV 面板名称手动补充。" or
    "声线目录来自已保存设置及手动补充，不能保证包含声库所有模式；请以当前声库面板为准。"}
  if curves.pitchCurve and not curves.pitchCurve.available then
    warnings[#warnings+1]=curves.pitchCurve.unavailableReason
  end
  return {projectFile=projectName(),groupName=group:getName(),groupUUID=group:getUUID(),
    groupOffset=ref:getTimeOffset(),groupPitchOffset=pitchOffset,notes=notes,noteCount=#notes,voiceFingerprint=voiceFingerprint,
    startSeconds=first and axis:getSecondsFromBlick(first) or nil,
    endSeconds=last and axis:getSecondsFromBlick(last) or nil,parameters=curves,
    capabilities={curves=true,nativePitch=curves.pitchCurve~=nil and curves.pitchCurve.available},
    capabilityWarnings=warnings}
end

local function registerVocalMode(args)
  -- 必须绑定最近读取的组与声线设置。名称由用户依据界面提供；getParameter 只验证
  -- 自动化通道可访问，不能证明它属于当前声库，因此返回 source=user 明确其来源。
  if type(args)~="table" or type(args.expected)~="table" then error("请先读取当前选区。") end
  for key in pairs(args) do if key~="name" and key~="expected" then error("补充声线参数包含未知字段。") end end
  local name=args.name
  if type(name)~="string" or #name==0 or #name>80 or name:find("%c") or name:match("^%s") or name:match("%s$") then
    error("请按面板填写完整声线名称，不能含控制字符或首尾空白，最长80字节。")
  end
  local before=selection()
  if before.noteCount==0 then error("请先选择音符并读取选区。") end
  for _,key in ipairs({"projectFile","groupUUID","groupOffset","groupPitchOffset","voiceFingerprint"}) do
    if args.expected[key]==nil or args.expected[key]~=before[key] then error("选区或声线设置已变化，请重新读取后补充。") end
  end
  if before.parameters["vocalMode_"..name] then return before end
  local editor,ref,group=current()
  local curve=group:getParameter("vocalMode_"..name)
  local definition=curve:getDefinition()
  if type(definition.range)~="table" or not finite(definition.range[1]) or not finite(definition.range[2]) then
    error("宿主未提供此声线自动化通道。")
  end
  local count=0
  for _,spec in pairs(before.parameters) do if spec.kind=="vocalMode" then count=count+1 end end
  if count>=64 then error("当前组声线目录已达到64项上限。") end
  local scope=fingerprint({before.projectFile,before.groupUUID,before.groupOffset,before.groupPitchOffset,before.voiceFingerprint})
  if not declaredModes[scope] then
    local contexts=0; for _ in pairs(declaredModes) do contexts=contexts+1 end
    if contexts>=64 then error("本次桥接会话的声线补充组数已达上限，请重启桥接后再试。") end
    declaredModes[scope]={}
  end
  declaredModes[scope][name]=true
  -- 能力目录改变也会使旧预览失效，避免沿用补充前的操作上下文。
  previews={}
  return selection()
end

local function getProject()
  local project=SV:getProject()
  local axis=project:getTimeAxis()
  local tracks={}
  for i=1,project:getNumTracks() do
    local track=project:getTrack(i)
    local groups={}
    for j=1,track:getNumGroups() do
      local ref=track:getGroupReference(j)
      if not ref:isInstrumental() then
        local g=ref:getTarget()
        groups[#groups+1]={index=j,name=g:getName(),uuid=g:getUUID(),noteCount=g:getNumNotes(),
          startSeconds=axis:getSecondsFromBlick(ref:getOnset()),endSeconds=axis:getSecondsFromBlick(ref:getEnd())}
      end
    end
    tracks[#tracks+1]={index=i,name=track:getName(),groups=groups}
  end
  local playback=SV:getPlayback()
  return {projectFile=projectName(),tracks=tracks,trackCount=#tracks,
    playhead=playback:getPlayhead(),playbackStatus=playback:getStatus(),hostVersion=SV:getHostInfo().hostVersion}
end

local function signature(s)
  -- 使用稳定的数组字段，避免依赖 JSON 对象键的遍历顺序。
  local rows={s.projectFile,s.groupUUID,s.groupOffset,s.groupPitchOffset,s.voiceFingerprint}
  -- 曲线采样以秒为单位；速度图变化后必须重新生成预览。
  for _,mark in ipairs(SV:getProject():getTimeAxis():getAllTempoMarks()) do
    rows[#rows+1]={mark.position,mark.bpm}
  end
  for _,n in ipairs(s.notes) do rows[#rows+1]={n.index,n.pitch,n.lyrics,n.onset,n.duration} end
  return json.encode(rows)
end

local function assertUnshared(uuid)
  local project=SV:getProject(); local references=0
  for i=1,project:getNumTracks() do
    local track=project:getTrack(i)
    for j=1,track:getNumGroups() do
      local ref=track:getGroupReference(j)
      if not ref:isInstrumental() and ref:getTarget():getUUID()==uuid then references=references+1 end
    end
  end
  if references>1 then error("此音符组被多个位置共用；本版本拒绝修改，以免同时影响其他片段。") end
end

local function validateOutsideCurve(curve,original,points,begin,finish,method)
  -- 只在线性/三次插值上使用分段采样判定；其他插值类型必须另外证明边界行为。
  -- 官方返回 Linear/Cubic；归一化只用于能力分支，指纹仍保存宿主原始值。
  local interpolationKind=type(method)=="string" and method:lower() or ""
  if interpolationKind~="linear" and interpolationKind~="cubic" then
    error("当前曲线插值方式尚未通过边界校验支持，未生成可应用预览。")
  end
  if #points>MAX_CURVE_POINTS then error("预览曲线超过4000个控制点，请缩短选区或先简化曲线。") end
  -- 克隆得到独立的 Automation，不挂接到任何音符组；模拟写入不改变用户工程。
  local candidate=curve:clone()
  candidate:removeAll()
  for _,point in ipairs(points) do candidate:add(point[1],point[2]) end
  if candidate:getInterpolationMethod()~=method then error("曲线克隆未保留插值方式，预览已拒绝。") end
  -- 宿主可能将值量化为 float32，并对同位置控制点去重；以后续读回值作为写入目标。
  local normalized=candidate:getAllPoints()
  if #normalized>MAX_CURVE_POINTS then error("宿主规范化后的曲线超过控制点上限。") end
  local unique={[begin]=begin,[finish]=finish}
  for _,source in ipairs({original,normalized}) do
    for _,point in ipairs(source) do unique[point[1]]=point[1] end
  end
  local breaks={}; for _,position in pairs(unique) do breaks[#breaks+1]=position end
  table.sort(breaks)
  -- 控制点两端的常值延伸也纳入检测，覆盖无原始控制点和单控制点的情况。
  local span=math.max(1,breaks[#breaks]-breaks[1])
  table.insert(breaks,1,breaks[1]-span)
  breaks[#breaks+1]=breaks[#breaks]+span
  for index=1,#breaks-1 do
    local left,right=breaks[index],breaks[index+1]
    if right<=begin or left>=finish then
      -- 原/新断点并集内，两条三次曲线的差仍为三次多项式。
      -- 每段检测端点及三个内部点；容差用于浮点舍入，任何外部变化均拒绝。
      for fraction=0,4 do
        local position=left+(right-left)*fraction/4
        local before,after=curve:get(position),candidate:get(position)
        if not finite(before) or not finite(after) or math.abs(before-after)>OUTSIDE_EPSILON then
          error("候选曲线会影响选区以外的插值，已拒绝预览；请扩大选区或手动调整边界。")
        end
      end
    end
  end
  return normalized,candidate
end

local function checkedCurve(args,limit)
  -- 只允许一种数值来源；数组维度、顺序、端点都由宿主再次核验。
  for key,_ in pairs(args) do
    if key~="parameter" and key~="delta" and key~="curve" and key~="renderMode" then error("预览包含未开放的字段。") end
  end
  if args.curve==nil then
    if not finite(args.delta) or args.delta==0 or math.abs(args.delta)>limit then error("调整量为零或超出本版本允许的小幅调整范围。") end
    return nil
  end
  if args.delta~=nil then error("曲线与统一调整量不能同时提供。") end
  local points=args.curve
  if type(points)~="table" or #points<2 or #points>64 then error("曲线需要2至64个有序点。") end
  local previous=-1; local nonzero=false
  for index,p in ipairs(points) do
    if type(p)~="table" or #p~=2 then error("每个曲线点必须是[位置比例,调整值]。") end
    for key,_ in pairs(p) do if key~=1 and key~=2 then error("曲线点包含未知字段。") end end
    if not finite(p[1]) or p[1]<0 or p[1]>1 or p[1]<=previous or not finite(p[2]) or math.abs(p[2])>limit then error("曲线位置或调整值超出允许范围。") end
    previous=p[1]; nonzero=nonzero or p[2]~=0
  end
  for key,_ in pairs(points) do if type(key)~="number" or key%1~=0 or key<1 or key>#points then error("曲线必须是连续数组。") end end
  if points[1][1]~=0 or points[#points][1]~=1 then error("曲线首尾位置必须是0和1。") end
  if not nonzero then error("曲线调整量全部为零。") end
  return points
end

local function simplifyPoints(points,tolerance,protected)
  -- 垂直误差受限的折线精简：使用显式栈，避免4000点递归导致宿主栈溢出。
  -- 区外点和边缘锚点永远保留，精简后的真实宿主插值还需独立校验。
  local keep={}; local anchors={}
  for index,p in ipairs(points) do
    if index==1 or index==#points or protected[p[1]] then keep[index]=true; anchors[#anchors+1]=index end
  end
  local stack={}; for index=1,#anchors-1 do stack[#stack+1]={anchors[index],anchors[index+1]} end
  while #stack>0 do
    local segment=table.remove(stack); local left,right=segment[1],segment[2]
    local a,b=points[left],points[right]; local maximum,selected=tolerance,nil
    for index=left+1,right-1 do
      local p=points[index]; local expected=a[2]+(b[2]-a[2])*(p[1]-a[1])/(b[1]-a[1])
      local difference=math.abs(p[2]-expected)
      if difference>maximum then maximum=difference; selected=index end
    end
    if selected then keep[selected]=true; stack[#stack+1]={left,selected}; stack[#stack+1]={selected,right} end
  end
  local result={}; for index,p in ipairs(points) do if keep[index] then result[#result+1]=p end end
  return result
end

local function preview(args)
  local name,delta=args.parameter,args.delta
  if type(name)~="string" then error("参数名称必须是字符串。") end
  local renderMode=args.renderMode==nil and "smooth" or args.renderMode
  if renderMode~="smooth" and renderMode~="points" then error("不支持的曲线表示方式。") end
  local s=selection()
  local descriptor=s.parameters[name]
  if descriptor and descriptor.pointCount>MAX_CURVE_POINTS then error("该曲线超过4000个控制点，请缩短或先整理工程。") end
  if not descriptor or descriptor.available==false then error("当前宿主没有返回可编辑的此参数，请重新读取选区。") end
  if s.noteCount==0 then error("请在钢琴卷帘中选中要调整的音符。") end
  if s.noteCount>128 then error("一次最多调整128个音符，请缩小选区。") end
  local _,ref,group,axis=current()
  assertUnshared(group:getUUID())
  local begin=s.notes[1].onset
  local finish=begin
  for _,n in ipairs(s.notes) do finish=math.max(finish,n.onset+n.duration) end
  local startSec=axis:getSecondsFromBlick(begin+ref:getTimeOffset())
  local endSec=axis:getSecondsFromBlick(finish+ref:getTimeOffset())
  if endSec<=startSec then error("选区持续时间必须大于零。") end
  if endSec-startSec>30 then error("一次最多调整30秒，请缩小选区。") end
  if name=="pitchCurve" then
    local native=NativePitch.preview(args,{group=group,ref=ref,axis=axis,selection=s,begin=begin,finish=finish,startSeconds=startSec,endSeconds=endSec})
    counter=counter+1; local id=session.."-"..counter
    previews={[id]={kind="pitch",signature=signature(s),parameter=name,before=native.before,after=native.after,created=os.time()}}
    local result=native.public
    result.previewId=id; result.parameter=name; result.renderMode="smooth"; result.representation="native-pitch-curve"
    result.label=descriptor.label; result.unit=descriptor.unit; result.noteCount=s.noteCount
    result.startSeconds=startSec; result.endSeconds=endSec; result.curve=args.curve; result.pointReduction=0
    return result
  end
  local knots=checkedCurve(args,descriptor.maxDelta)
  local curve=group:getParameter(name)
  local original=curve:getAllPoints()
  if #original>MAX_CURVE_POINTS then error("该曲线超过4000个控制点，请先在工程副本中简化后再使用。") end
  local interpolation=curve:getInterpolationMethod()
  local range=curve:getDefinition().range
  local map,protected={},{}
  local clipped=false
  local fade=math.min(0.08,(endSec-startSec)/4)
  local function offset(ratio)
    if not knots then return delta end
    for index=1,#knots-1 do
      local a,b=knots[index],knots[index+1]
      if ratio<=b[1] then return a[2]+(b[2]-a[2])*(ratio-a[1])/(b[1]-a[1]) end
    end
    return knots[#knots][2]
  end
  -- 保存区间外的控制点；区间内保留原有点并叠加有淡入淡出的偏移。
  local function transformed(b)
    local t=axis:getSecondsFromBlick(b+ref:getTimeOffset())
    local envelope=math.max(0,math.min(1,(t-startSec)/fade,(endSec-t)/fade))
    local value=curve:get(b)+offset(math.max(0,math.min(1,(t-startSec)/(endSec-startSec))))*envelope
    if value<range[1] or value>range[2] then clipped=true end
    return math.max(range[1],math.min(range[2],value))
  end
  for _,p in ipairs(original) do
    -- 数字键会合并数学上相同的整数/浮点位置，避免 1000 与 1000.0 产生重复点。
    map[p[1]]={p[1],(p[1]>=begin and p[1]<=finish) and transformed(p[1]) or p[2]}
    if p[1]<begin or p[1]>finish then protected[p[1]]=true end
  end
  for k=0,math.ceil((endSec-startSec)/0.02) do
    local t=math.min(endSec,startSec+k*0.02)
    local b=math.floor(axis:getBlickFromSeconds(t)-ref:getTimeOffset()+0.5)
    map[b]={b,transformed(b)}
  end
  -- 保留淡入/淡出转折与用户曲线节点，平台速度变化时也按真实秒数换算。
  local anchors={startSec+fade,endSec-fade}
  for _,point in ipairs(knots or {}) do anchors[#anchors+1]=startSec+(endSec-startSec)*point[1] end
  for _,t in ipairs(anchors) do
    local b=math.floor(axis:getBlickFromSeconds(t)-ref:getTimeOffset()+0.5)
    map[b]={b,transformed(b)}; protected[b]=true
    -- 转折两侧各保留约1毫秒的锚点，约束三次插值的左右切线。
    -- 否则20毫秒采样在快速淡入/淡出处可能始终过冲，增加均匀点数也难以收敛。
    -- 不用1 blick的极短间距：真实宿主以float32保存参数，量化误差除以极短
    -- 时间会放大成异常切线，尤其是叠加到已有气声/张力曲线时。
    -- 这些短间隔锚点只存在于选区内部，区外仍由独立宿主克隆完整检查。
    if b>begin+1 and b<finish-1 then
      for _,seconds in ipairs({t-0.001,t+0.001}) do
        local edge=math.floor(axis:getBlickFromSeconds(seconds)-ref:getTimeOffset()+0.5)
        if edge>begin+1 and edge<finish-1 then map[edge]={edge,transformed(edge)}; protected[edge]=true end
      end
    end
  end
  -- 内外各一blick锚点阻止三次插值把边缘切线传播到选区之外。
  -- 极短区间无法容纳成对锚点时直接拒绝，不能制造反向顺序。
  if finish-begin<4 then error("选区过短，无法建立可靠的曲线边缘。") end
  for _,b in ipairs({begin-1,begin,begin+1,finish-1,finish,finish+1}) do map[b]={b,curve:get(b)}; protected[b]=true end
  local points={}; for _,p in pairs(map) do points[#points+1]=p end
  table.sort(points,function(a,b) return a[1]<b[1] end)
  if #points>MAX_CURVE_POINTS then error("候选曲线超过4000个控制点，请缩短选区。") end
  local denseCount=#points
  local tolerance=parameterSpecs[name] and parameterSpecs[name].tolerance or 0.15
  local candidate,normalized,lastError
  -- 精简仅减少本次选区内的点；复杂插值若不能满足误差要求，逐步提高精度。
  -- 不调用真实组上的 simplify，也不改变整条自动化曲线的插值类型。
  for attempt=1,(renderMode=="smooth" and 10 or 1) do
    local proposed=renderMode=="smooth" and simplifyPoints(points,tolerance/2^attempt,protected) or points
    local ok,result,copy=pcall(validateOutsideCurve,curve,original,proposed,begin,finish,interpolation)
    if ok then
      local accurate=true
      local badSegments,segment={},1
      for index=1,#points-1 do
        local a,b=points[index],points[index+1]
        if a[1]>=begin+1 and b[1]<=finish-1 then
          for part=0,4 do
            local position=a[1]+(b[1]-a[1])*part/4
            local value=copy:get(position)
            if not finite(value) or math.abs(value-transformed(position))>tolerance or value<range[1]-OUTSIDE_EPSILON or value>range[2]+OUTSIDE_EPSILON then
              accurate=false
              -- 每个精简片段只加回误差最大的相邻采样点，而非把所有超差点一齐
              -- 固定下来。三次插值的一次弯曲可能让整段轻微超差，后者会立刻退化
              -- 为密集控制点；逐段自适应细分能保留相同精度并显著减少最终点数。
              while segment<#proposed-1 and position>proposed[segment+1][1] do segment=segment+1 end
              local difference=finite(value) and math.abs(value-transformed(position)) or math.huge
              if not badSegments[segment] or difference>badSegments[segment].difference then
                badSegments[segment]={difference=difference,left=a[1],right=b[1]}
              end
            end
          end
        end
      end
      if accurate then normalized,candidate=result,copy; break end
      for _,bad in pairs(badSegments) do protected[bad.left]=true; protected[bad.right]=true end
      lastError="精简曲线无法在当前插值方式下达到误差要求。"
    else lastError=result end
  end
  if not candidate then error(tostring(lastError).." 未写入；可缩短选区或选择控制点模式。") end
  points=normalized
  local view={}
  for index=0,96 do
    local ratio=index/96; local t=startSec+(endSec-startSec)*ratio
    local b=axis:getBlickFromSeconds(t)-ref:getTimeOffset()
    view[#view+1]={position=ratio,before=curve:get(b),after=candidate:get(b)}
  end
  counter=counter+1; local id=session.."-"..counter
  -- 仅保留最近一次预览，避免长时间保留宿主对象及无界增长。
  previews={}
  previews[id]={kind="automation",signature=signature(s),parameter=name,before=original,after=points,interpolation=interpolation,created=os.time()}
  return {previewId=id,parameter=name,delta=delta,noteCount=s.noteCount,startSeconds=startSec,endSeconds=endSec,
    summary="将在所选音符覆盖的连续时间段叠加参数曲线，边缘淡入淡出；尚未写入。",
    curve=knots,renderMode=renderMode,representation=renderMode=="smooth" and "automation-simplified" or "automation-points",
    label=descriptor.label,unit=descriptor.unit,beforePointCount=#original,pointCount=#points,
    pointReduction=math.max(0,denseCount-#points),curvePreview=view,
    capabilityWarnings=clipped and {"部分目标值达到参数范围边界，预览已按宿主范围限制。"} or {}}
end

local function writePoints(curve,points)
  curve:removeAll()
  for _,p in ipairs(points) do curve:add(p[1],p[2]) end
end

local function writeVerified(curve,points)
  -- 写入与读回验证同属一个失败边界；没有抛出宿主异常不等于写入结果正确。
  writePoints(curve,points)
  if not samePoints(curve:getAllPoints(),points) then error("写入后控制点校验失败。") end
end

local function requireWrite()
  if not writeProject or writeProject~=projectName() then error("当前为只读模式，请先开启“允许修改选区”。") end
end

local function editTarget(record,group)
  return record.kind=="pitch" and group or group:getParameter(record.parameter)
end

local function sameSnapshot(record,target,snapshot)
  if record.kind=="pitch" then return NativePitch.same(target,snapshot) end
  return samePoints(target:getAllPoints(),snapshot)
end

local function writeSnapshot(record,target,snapshot)
  -- 原生音高与自动化都通过同一撤销/回滚边界，禁止发生第二套隐式应用流程。
  if record.kind=="pitch" then NativePitch.write(target,snapshot) else writeVerified(target,snapshot) end
end

local function captureSnapshot(record,target)
  if record.kind=="pitch" then return NativePitch.snapshot(target) end
  return target:getAllPoints()
end

local function apply(args)
  requireWrite()
  local p=previews[args.previewId]
  if not p or os.time()-p.created>300 then error("预览不存在或已过期，请重新预览。") end
  local s=selection()
  if signature(s)~=p.signature then error("选区或音符已变化，请重新读取并预览。") end
  local _,_,group=current(); assertUnshared(group:getUUID())
  local curve=editTarget(p,group)
  if p.kind~="pitch" and curve:getInterpolationMethod()~=p.interpolation then error("曲线插值方式已经改变，请重新预览。") end
  if not sameSnapshot(p,curve,p.before) then error("原参数曲线已经改变，请重新预览。") end
  local previousEdit=lastEdit
  local edit={kind=p.kind,project=projectName(),uuid=group:getUUID(),parameter=p.parameter,before=p.before,after=p.after,
    interpolation=p.interpolation,voiceFingerprint=s.voiceFingerprint,groupOffset=s.groupOffset,groupPitchOffset=s.groupPitchOffset}
  SV:getProject():newUndoRecord()
  local ok,err=pcall(writeSnapshot,p,curve,p.after)
  if not ok then
    previews={}
    local recovered=pcall(writeSnapshot,p,curve,p.before)
    if recovered then
      lastEdit=previousEdit
      error("应用失败："..tostring(err).."；已恢复并校验原控制点。")
    end
    -- 回滚也失败时，记录当前残留状态；restore 仍必须先验证它未再被用户改变。
    local readable,remaining=pcall(captureSnapshot,p,curve)
    edit.after=readable and remaining or nil
    edit.recoveryPending=true
    lastEdit=edit
    error("应用失败："..tostring(err).."；恢复失败，已保留恢复记录，请立即在SynthV撤销或使用恢复功能。")
  end
  lastEdit=edit
  previews={}
  return {verified=true,parameter=p.parameter,pointCount=p.kind=="pitch" and p.after.pointCount or #p.after,
    undoRecords=1,message="参数已应用，可在SynthV试听或撤销。"}
end

local function restore()
  requireWrite()
  if not lastEdit or lastEdit.project~=projectName() then error("本会话没有可恢复的修改。") end
  local _,_,group=current()
  if group:getUUID()~=lastEdit.uuid then error("请先回到上次修改的音符组。") end
  assertUnshared(group:getUUID())
  local s=selection()
  if s.voiceFingerprint~=lastEdit.voiceFingerprint or s.groupOffset~=lastEdit.groupOffset or s.groupPitchOffset~=lastEdit.groupPitchOffset then error("声线设置或音符组偏移已变化，无法安全覆盖；请使用 SynthV 撤销。") end
  local curve=editTarget(lastEdit,group)
  if lastEdit.kind~="pitch" and curve:getInterpolationMethod()~=lastEdit.interpolation then error("曲线插值方式已经改变，无法安全恢复。") end
  if not lastEdit.after then error("无法读取上次失败后的曲线状态，请直接在SynthV撤销。") end
  if not sameSnapshot(lastEdit,curve,lastEdit.after) then error("曲线已被手动修改或撤销，无法安全覆盖。") end
  SV:getProject():newUndoRecord()
  local ok,err=pcall(writeSnapshot,lastEdit,curve,lastEdit.before)
  if not ok then
    -- 恢复本身失败也不得丢失原始数据，更新残留状态以允许后续安全恢复。
    local readable,remaining=pcall(captureSnapshot,lastEdit,curve)
    lastEdit.after=readable and remaining or nil
    lastEdit.recoveryPending=true
    error("恢复失败，已保留原控制点和恢复记录，请检查SynthV撤销历史："..tostring(err))
  end
  lastEdit=nil
  return {verified=true,message="已恢复本助手上一次修改前的完整参数控制点。"}
end

local function dispatch(action,args)
  if action=="get_project" then return getProject()
  elseif action=="get_selection" then return selection()
  elseif action=="register_vocal_mode" then return registerVocalMode(args)
  elseif action=="preview" then return preview(args)
  elseif action=="apply" then return apply(args)
  elseif action=="restore" then return restore()
  elseif action=="write_mode" then
    local name=projectName()
    if args.enabled and name=="" then error("请先保存工程副本。") end
    if args.enabled and name~=args.expectedProject then error("工程已切换，未开启写入；请重新备份当前工程。") end
    writeProject=args.enabled and name or nil
    heartbeat(); return {writeEnabled=writeProject~=nil}
  elseif action=="play_segment" then
    local start,duration=args.startSeconds,args.durationSeconds
    if not finite(start) or start<0 or not finite(duration) or duration<1 or duration>30 then error("试听范围无效。") end
    local play=SV:getPlayback()
    if play:getStatus()~="stopped" then error("请先停止当前播放，再录制片段。") end
    local previous=play:getPlayhead()
    playbackToken=playbackToken+1; local token=playbackToken
    play:seek(start); play:play()
    SV:setTimeout(math.floor(duration*1000),function()
      if token==playbackToken then play:stop(); play:seek(previous) end
    end)
    return {started=true,startSeconds=start,durationSeconds=duration,previousPlayhead=previous}
  elseif action=="stop_playback" then
    playbackToken=playbackToken+1; SV:getPlayback():stop(); return {stopped=true}
  elseif action=="stop_bridge" then
    stopping=true
    writeProject=nil; writeFile("heartbeat.json",{session=session,timestamp=0,writeEnabled=false}); SV:finish()
    return {stopped=true}
  end
  error("未知或未开放的桥接动作。")
end

local function poll()
  ticks=ticks+1
  if ticks%20==1 then pcall(heartbeat) end
  local raw=readFile("request.json")
  if raw then
    -- 只有成功取得请求文件所有权才能执行；移动失败时保留请求，禁止重复分发。
    local claimed=os.rename(IPC_DIR.."/request.json",IPC_DIR.."/processing.json")
    if claimed then
      local ok,request=pcall(json.decode,raw)
      if ok and type(request)=="table" then
      local success,result=pcall(function()
        if request.session~=session then error("桥接会话已变化，请重新读取。") end
        if not finite(request.expires) or os.time()>request.expires then error("请求已过期，未执行。") end
        return dispatch(request.action,request.args or {})
      end)
      pcall(writeFile,"response.json",{id=request.id,ok=success,result=success and result or nil,error=not success and tostring(result) or nil})
      end
      os.remove(IPC_DIR.."/processing.json")
    end
  end
  if not stopping then SV:setTimeout(50,poll) end
end

function main()
  -- 新实例启动前检查心跳，避免两个脚本同时处理同一个请求。
  local previous=readFile("heartbeat.json")
  if previous then
    local ok,h=pcall(json.decode,previous)
    if ok and h.timestamp and os.time()-h.timestamp<5 then
      SV:showMessageBox("SynthV Assistant","助手脚本已在运行。无需重复启动。")
      SV:finish(); return
    end
  end
  os.remove(IPC_DIR.."/request.json"); os.remove(IPC_DIR.."/processing.json")
  heartbeat(); poll()
end
