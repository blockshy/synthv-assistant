--[[
SynthV Assistant：面向 Studio 2.2.1 的小范围桥接。
只执行下方白名单中的动作，不执行来自模型的 Lua/系统命令。
参数修改采用“预览 → 检查选区和原始曲线 → 应用”的流程；默认只读。
JSON 解析器及 IPC_DIR 由 Python 安装器注入本文件前部。
]]

function getClientInfo()
  return {name="SynthV Assistant", category="SynthV Assistant", author="Local", versionNumber=1, minEditorVersion=0x020102}
end

local session = tostring(os.time()) .. "-" .. tostring(math.floor(os.clock()*1000000))
local writeProject = nil
local previews, lastEdit = {}, nil
local counter, ticks, playbackToken = 0, 0, 0
local stopping=false
local allowed = {breathiness=true, tension=true, loudness=true, gender=true, pitchDelta=true}
local limits = {breathiness=0.3, tension=0.3, loudness=6, gender=0.3, pitchDelta=100}
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

local function projectName()
  return SV:getProject():getFileName() or ""
end

local function heartbeat()
  local info=SV:getHostInfo()
  writeFile("heartbeat.json", {session=session,timestamp=os.time(),hostVersion=info.hostVersion,
    projectFile=projectName(),writeEnabled=writeProject~=nil and writeProject==projectName(),protocol=1})
end

local function current()
  local editor=SV:getMainEditor()
  local ref=editor:getCurrentGroup()
  if not ref then error("请先在 SynthV 打开一个音符组。") end
  return editor,ref,ref:getTarget(),SV:getProject():getTimeAxis()
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
  local curves={}
  for name,_ in pairs(allowed) do
    local param=group:getParameter(name)
    local definition=param:getDefinition()
    curves[name]={pointCount=#param:getAllPoints(),range=definition.range,defaultValue=definition.defaultValue}
  end
  return {projectFile=projectName(),groupName=group:getName(),groupUUID=group:getUUID(),
    groupOffset=ref:getTimeOffset(),notes=notes,noteCount=#notes,
    startSeconds=first and axis:getSecondsFromBlick(first) or nil,
    endSeconds=last and axis:getSecondsFromBlick(last) or nil,parameters=curves}
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
  local rows={s.projectFile,s.groupUUID,s.groupOffset}
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
  return normalized
end

local function preview(args)
  local name,delta=args.parameter,args.delta
  if not allowed[name] then error("此参数不在可编辑白名单中。") end
  if not finite(delta) or delta==0 or math.abs(delta)>limits[name] then error("调整量为零或超出本版本允许的小幅调整范围。") end
  local s=selection()
  if s.noteCount==0 then error("请在钢琴卷帘中选中要调整的音符。") end
  if s.noteCount>128 then error("一次最多调整128个音符，请缩小选区。") end
  local _,ref,group,axis=current()
  assertUnshared(group:getUUID())
  local curve=group:getParameter(name)
  local original=curve:getAllPoints()
  if #original>MAX_CURVE_POINTS then error("该曲线超过4000个控制点，请先在工程副本中简化后再使用。") end
  local interpolation=curve:getInterpolationMethod()
  local begin=s.notes[1].onset
  local finish=begin
  for _,n in ipairs(s.notes) do finish=math.max(finish,n.onset+n.duration) end
  local startSec=axis:getSecondsFromBlick(begin+ref:getTimeOffset())
  local endSec=axis:getSecondsFromBlick(finish+ref:getTimeOffset())
  if endSec<=startSec then error("选区持续时间必须大于零。") end
  if endSec-startSec>30 then error("一次最多调整30秒，请缩小选区。") end
  local range=curve:getDefinition().range
  local map={}
  -- 保存区间外的控制点；区间内保留原有点并叠加有淡入淡出的偏移。
  local function transformed(b)
    local t=axis:getSecondsFromBlick(b+ref:getTimeOffset())
    local fade=math.min(0.08,(endSec-startSec)/4)
    local envelope=math.max(0,math.min(1,(t-startSec)/fade,(endSec-t)/fade))
    local value=curve:get(b)+delta*envelope
    return math.max(range[1],math.min(range[2],value))
  end
  for _,p in ipairs(original) do
    -- 数字键会合并数学上相同的整数/浮点位置，避免 1000 与 1000.0 产生重复点。
    map[p[1]]={p[1],(p[1]>=begin and p[1]<=finish) and transformed(p[1]) or p[2]}
  end
  for k=0,math.ceil((endSec-startSec)/0.02) do
    local t=math.min(endSec,startSec+k*0.02)
    local b=math.floor(axis:getBlickFromSeconds(t)-ref:getTimeOffset()+0.5)
    map[b]={b,transformed(b)}
  end
  for _,b in ipairs({begin-1,begin,finish,finish+1}) do map[b]={b,curve:get(b)} end
  local points={}; for _,p in pairs(map) do points[#points+1]=p end
  table.sort(points,function(a,b) return a[1]<b[1] end)
  points=validateOutsideCurve(curve,original,points,begin,finish,interpolation)
  counter=counter+1; local id=session.."-"..counter
  -- 仅保留最近一次预览，避免长时间保留宿主对象及无界增长。
  previews={}
  previews[id]={signature=signature(s),parameter=name,before=original,after=points,interpolation=interpolation,created=os.time()}
  return {previewId=id,parameter=name,delta=delta,noteCount=s.noteCount,startSeconds=startSec,endSeconds=endSec,
    summary="将在所选音符覆盖的连续时间段叠加参数偏移，边缘淡入淡出；尚未写入。",pointCount=#points}
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
  if not writeProject or writeProject~=projectName() then error("当前为只读模式，请先保存工程副本并开启编辑。") end
end

local function apply(args)
  requireWrite()
  local p=previews[args.previewId]
  if not p or os.time()-p.created>300 then error("预览不存在或已过期，请重新预览。") end
  local s=selection()
  if signature(s)~=p.signature then error("选区或音符已变化，请重新读取并预览。") end
  local _,_,group=current(); assertUnshared(group:getUUID())
  local curve=group:getParameter(p.parameter)
  if curve:getInterpolationMethod()~=p.interpolation then error("曲线插值方式已经改变，请重新预览。") end
  if not samePoints(curve:getAllPoints(),p.before) then error("原参数曲线已经改变，请重新预览。") end
  local previousEdit=lastEdit
  local edit={project=projectName(),uuid=group:getUUID(),parameter=p.parameter,before=p.before,after=p.after,interpolation=p.interpolation}
  SV:getProject():newUndoRecord()
  local ok,err=pcall(writeVerified,curve,p.after)
  if not ok then
    previews={}
    local recovered=pcall(writeVerified,curve,p.before)
    if recovered then
      lastEdit=previousEdit
      error("应用失败："..tostring(err).."；已恢复并校验原控制点。")
    end
    -- 回滚也失败时，记录当前残留状态；restore 仍必须先验证它未再被用户改变。
    local readable,remaining=pcall(function() return curve:getAllPoints() end)
    edit.after=readable and remaining or nil
    edit.recoveryPending=true
    lastEdit=edit
    error("应用失败："..tostring(err).."；恢复失败，已保留恢复记录，请立即在SynthV撤销或使用恢复功能。")
  end
  lastEdit=edit
  previews={}
  return {verified=true,parameter=p.parameter,pointCount=#p.after,undoRecords=1,message="参数已应用，可在SynthV试听或撤销。"}
end

local function restore()
  requireWrite()
  if not lastEdit or lastEdit.project~=projectName() then error("本会话没有可恢复的修改。") end
  local _,_,group=current()
  if group:getUUID()~=lastEdit.uuid then error("请先回到上次修改的音符组。") end
  assertUnshared(group:getUUID())
  local curve=group:getParameter(lastEdit.parameter)
  if curve:getInterpolationMethod()~=lastEdit.interpolation then error("曲线插值方式已经改变，无法安全恢复。") end
  if not lastEdit.after then error("无法读取上次失败后的曲线状态，请直接在SynthV撤销。") end
  if not samePoints(curve:getAllPoints(),lastEdit.after) then error("曲线已被手动修改或撤销，无法安全覆盖。") end
  SV:getProject():newUndoRecord()
  local ok,err=pcall(writeVerified,curve,lastEdit.before)
  if not ok then
    -- 恢复本身失败也不得丢失原始数据，更新残留状态以允许后续安全恢复。
    local readable,remaining=pcall(function() return curve:getAllPoints() end)
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
