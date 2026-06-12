---
rule_kind: decisionRule
intent: classify the subject's nutritional status (WFA/HFA/WFH) from height, weight,
  age, sex, and flag growth faltering when weight gain since the prior visit is low
entity_param: programEncounter
encounter_types: []
concepts:
- Height
- Weight
source_org: JSSCP
---
```js
"use strict";
 ({params, imports}) => {
  const programEncounter = params.entity;
  const decisions = params.decisions;
  const _ = imports.lodash;
  const weight = programEncounter.getObservationValue("Weight");
  const height = programEncounter.getObservationValue("Height");
  const asOnDate = programEncounter.encounterDateTime;
  const individual = programEncounter.programEnrolment.individual;
 

  const zScoreGradeStatusMappingWeightForAge = {
  '1': 'Normal',
  '2': 'Moderately Underweight',
  '3': 'Severely Underweight'
  };
 

  const zScoreGradeStatusMappingHeightForAge = {
  '1': 'Normal',
  '2': 'Stunted',
  '3': 'Severely stunted'
  };
 

  const zScoreGradeStatusMappingWeightForHeight = [
  ["Severely wasted", -3],
  ["Wasted", -2],
  ["Normal", 1],
  ["Possible risk of overweight", 2],
  ["Overweight", 3],
  ["Obese", Infinity],
  ];
 

  const weightForHeightStatus = function (zScore) {
  let found = _.find(zScoreGradeStatusMappingWeightForHeight, function (currentStatus) {
  return zScore <= currentStatus[1];
  });
  return found && found[0];
  };
 

  const getGradeforZscore = (zScore) => {
  let grade;
  if (zScore <= -3) {
  grade = 3;
  } else if (zScore > -3 && zScore < -2) {
  grade = 2;
  } else if (zScore >= -2) {
  grade = 1;
  }
 

  return grade;
 

  };
 

  const addIfRequired = (decisions, name, value) => {
  if (value === -0) value = 0;
  if (value !== undefined) decisions.push({name: name, value: value});
  };
 

  const zScoresForChild = imports.common.getZScore(individual, asOnDate, weight, height);
 

  const wfaGrade = getGradeforZscore(zScoresForChild.wfa);
  const wfaStatus = zScoreGradeStatusMappingWeightForAge[wfaGrade];
  const hfaGrade = getGradeforZscore(zScoresForChild.hfa);
  const hfaStatus = zScoreGradeStatusMappingHeightForAge[hfaGrade];
  const wfhStatus = weightForHeightStatus(zScoresForChild.wfh);
 

  addIfRequired(decisions.encounterDecisions, "Weight for age z-score", zScoresForChild.wfa);
  addIfRequired(decisions.encounterDecisions, "Weight for age Grade", wfaGrade);
  addIfRequired(decisions.encounterDecisions, "Weight for age Status", wfaStatus ? [wfaStatus] : []);
 

  addIfRequired(decisions.encounterDecisions, "Height for age z-score", zScoresForChild.hfa);
  addIfRequired(decisions.encounterDecisions, "Height for age Grade", hfaGrade);
  addIfRequired(decisions.encounterDecisions, "Height for age Status", hfaStatus ? [hfaStatus] : []);
 

  addIfRequired(decisions.encounterDecisions, "Weight for height z-score", zScoresForChild.wfh);
  addIfRequired(decisions.encounterDecisions, "Weight for Height Status", wfhStatus ? [wfhStatus] : []);
  
  
  // ------------------------------
  // Growth Faltering Calculation
  // ------------------------------
  // Fetch all past encounters for this enrolment
  const pastEncounters = imports.common.getEncounters(individual, programEncounter.programEnrolment.program, 'Weight', {before: asOnDate});
  
  // Sort encounters by date descending
  const sortedEncounters = _.orderBy(pastEncounters, ['encounterDateTime'], ['desc']);
 

  if (sortedEncounters.length >= 1) {
  // The most recent past encounter is the 1st element
  const prevEncounter = sortedEncounters[0];
  const prevWeight = prevEncounter.getObservationValue("Weight");
 

  if (prevWeight !== undefined && prevWeight !== null) {
  const weightGain = weight - prevWeight; // kg
  const growthFaltering = weightGain < 0.3 ? 'Yes' : 'No';
 

  addIfRequired(decisions.encounterDecisions, "Previous Encounter Weight", prevWeight);
  addIfRequired(decisions.encounterDecisions, "Weight Gain since last encounter (kg)", weightGain);
  addIfRequired(decisions.encounterDecisions, "Growth Faltering", growthFaltering);
  }
  } else {
  addIfRequired(decisions.encounterDecisions, "Growth Faltering", 'Insufficient data');
  }
 

  return decisions;
 };
```
